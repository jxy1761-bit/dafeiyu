"""做梦预览 / 执行 / 操作日志 / 撤销工具。

原则：先预览、再确认、可撤销；高分条目不会进入候选。
"""

from ..memory_scope import adapter_access
import asyncio
import json
from dataclasses import field
from typing import Any

from pydantic.dataclasses import dataclass

from astrbot.api import logger
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import FunctionTool, ToolExecResult
from astrbot.core.astr_agent_context import AstrAgentContext

from .agentic_recall_common import resolve_tool_access


def _json_result(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, default=str)


async def _gate(config_manager: Any, plugin_context: Any, context: Any):
    event, _session_id, _persona_id, access_error = await resolve_tool_access(
        config_manager, plugin_context, context
    )
    if access_error is not None:
        return event, access_error
    is_admin = getattr(event, "is_admin", None)
    try:
        allowed = bool(is_admin()) if callable(is_admin) else False
    except Exception:
        allowed = False
    if not allowed:
        try:
            allowed = (
                event.get_platform_name() == "webchat" and event.is_private_chat()
            )
        except Exception:
            allowed = False
    if not allowed:
        return event, {"error": "admin permission required"}
    try:
        if not event.is_private_chat():
            return event, {"error": "memory operations are private-chat only"}
    except Exception:
        pass
    return event, None


@dataclass
class MemoryDreamPreviewTool(FunctionTool[AstrAgentContext]):
    """生成一份做梦计划，不写入任何记忆。"""

    __pydantic_config__ = {"arbitrary_types_allowed": True}

    context: Any = None
    config_manager: Any = None
    memory_engine: Any = None
    consolidation_manager: Any = None

    name: str = "dream_preview"
    description: str = (
        "生成一次“做梦”整理计划，但先不改记忆：列出会动哪几条、每条摘要、"
        "合并后的大致内容、哪些原档会归档，以及哪些高分条目被保护跳过。"
        "把 op_id 和报告给主人看；主人确认后再调用 dream_apply。"
        "只有管理员私聊可用。"
    )
    parameters: dict[str, Any] = field(
        default_factory=lambda: {"type": "object", "properties": {}, "required": []}
    )

    async def call(self, context: ContextWrapper[AstrAgentContext]) -> ToolExecResult:
        event, error = await _gate(self.config_manager, self.context, context)
        if error:
            return _json_result({"ok": False, **error})
        if self.consolidation_manager is None:
            return _json_result({"ok": False, "error": "consolidation manager is not ready"})
        with adapter_access(self.config_manager, event):
            try:
                result = await self.consolidation_manager.preview_consolidation()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("[DreamPreview] 预览失败: %s", exc, exc_info=True)
                return _json_result({"ok": False, "error": str(exc)})
        return _json_result(
            {
                "ok": True,
                "op_id": result.get("op_id"),
                "candidates": result.get("candidates", 0),
                "groups": result.get("groups", 0),
                "report": result.get("report", ""),
            }
        )


@dataclass
class MemoryDreamRunTool(FunctionTool[AstrAgentContext]):
    """自动做梦：先出报告，模型自行判断后在安全范围内直接定稿。"""

    __pydantic_config__ = {"arbitrary_types_allowed": True}

    context: Any = None
    config_manager: Any = None
    memory_engine: Any = None
    consolidation_manager: Any = None

    name: str = "dream_run"
    description: str = (
        "自动做梦：先生成完整报告，再根据安全规则决定是否直接定稿。"
        "默认 auto_apply=true：只有低分旧记忆、条数不超过上限、没有失败时才会自动执行；"
        "高分条目永远不参与；超过上限或 auto_apply=false 时只返回报告和 op_id，不写记忆。"
        "报告会写进 memory_ops，事后可用 memory_op_undo 撤销。"
        "主人说“你自己整理一下记忆/做个梦”时用，不需要再让主人确认。"
        "只有管理员私聊可用。"
    )
    parameters: dict[str, Any] = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "auto_apply": {
                    "type": "boolean",
                    "description": "true=报告正常就直接执行；false=只预览。默认 true。",
                    "default": True,
                }
            },
            "required": [],
        }
    )

    async def call(
        self,
        context: ContextWrapper[AstrAgentContext],
        auto_apply: bool = True,
    ) -> ToolExecResult:
        event, error = await _gate(self.config_manager, self.context, context)
        if error:
            return _json_result({"ok": False, **error})
        if self.consolidation_manager is None:
            return _json_result({"ok": False, "error": "consolidation manager is not ready"})
        with adapter_access(self.config_manager, event):
            try:
                try:
                    dream_groups = int(
                        self.config_manager.get(
                            "memory_dream.max_groups_per_run", 3
                        )
                        or 3
                    )
                except (TypeError, ValueError):
                    dream_groups = 3
                preview = await self.consolidation_manager.preview_consolidation(
                    max_groups=dream_groups
                )
                plans = preview.get("plans") or []
                config_supersedes = preview.get("config_supersedes") or []
                op_id = preview.get("op_id")
                if not op_id or (not plans and not config_supersedes):
                    return _json_result(
                        {
                            "ok": True,
                            "applied": False,
                            "op_id": None,
                            "report": preview.get("report", ""),
                            "reason": "没有可做梦的低分旧记忆或配置冲突",
                        }
                    )

                total_items = sum(len(p.get("old_ids") or []) for p in plans)
                total_items += len(config_supersedes)
                try:
                    limit = int(
                        self.config_manager.get(
                            "agent_tools.dream_auto_max_items", 20
                        )
                        or 20
                    )
                except (TypeError, ValueError):
                    limit = 20
                too_large = total_items > max(1, limit)
                if not auto_apply or too_large:
                    reason = (
                        f"本轮 {total_items} 条超过自动定稿上限 {limit} 条，只出报告"
                        if too_large
                        else "auto_apply=false，只出报告"
                    )
                    return _json_result(
                        {
                            "ok": True,
                            "applied": False,
                            "op_id": op_id,
                            "candidates": preview.get("candidates", 0),
                            "groups": len(plans),
                            "config_supersedes": len(config_supersedes),
                            "items": total_items,
                            "report": preview.get("report", ""),
                            "reason": reason,
                            "note": "报告已生成；模型可自行决定是否调用 dream_apply 定稿。",
                        }
                    )

                applied = await self.consolidation_manager.apply_consolidation(int(op_id))
                return _json_result(
                    {
                        "ok": bool(applied.get("ok")),
                        "applied": True,
                        "op_id": op_id,
                        "items": total_items,
                        "config_superseded": applied.get("config_superseded", 0),
                        "report": applied.get("report", preview.get("report", "")),
                        "errors": applied.get("errors") or [],
                    }
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("[DreamRun] 自动做梦失败: %s", exc, exc_info=True)
                return _json_result({"ok": False, "error": str(exc)})


@dataclass
class MemoryDreamApplyTool(FunctionTool[AstrAgentContext]):
    """执行一次已经预览过的做梦计划。"""

    __pydantic_config__ = {"arbitrary_types_allowed": True}

    context: Any = None
    config_manager: Any = None
    memory_engine: Any = None
    consolidation_manager: Any = None

    name: str = "dream_apply"
    description: str = (
        "执行 dream_preview 生成的计划。op_id 必须是预览返回的那个；"
        "执行后旧档归档，可用 memory_op_undo 按 op_id 撤销。"
        "只有管理员私聊可用；主人没确认前不要调用。"
    )
    parameters: dict[str, Any] = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "op_id": {
                    "type": "integer",
                    "description": "dream_preview 返回的 op_id。",
                }
            },
            "required": ["op_id"],
        }
    )

    async def call(
        self, context: ContextWrapper[AstrAgentContext], op_id: int
    ) -> ToolExecResult:
        event, error = await _gate(self.config_manager, self.context, context)
        if error:
            return _json_result({"ok": False, **error})
        if self.consolidation_manager is None:
            return _json_result({"ok": False, "error": "consolidation manager is not ready"})
        with adapter_access(self.config_manager, event):
            try:
                result = await self.consolidation_manager.apply_consolidation(int(op_id))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("[DreamApply] 执行失败: %s", exc, exc_info=True)
                return _json_result({"ok": False, "error": str(exc)})
        return _json_result(result)


@dataclass
class MemoryOpListTool(FunctionTool[AstrAgentContext]):
    """列出最近的记忆操作。"""

    __pydantic_config__ = {"arbitrary_types_allowed": True}

    context: Any = None
    config_manager: Any = None
    memory_engine: Any = None

    name: str = "memory_op_list"
    description: str = (
        "列出最近的记忆操作（做梦、批量归档、批量更新、dedup 合并），"
        "包含 op_id、类型、状态、摘要。用于回答“上次做梦动了哪几条/能不能还原”。"
        "只有管理员私聊可用。"
    )
    parameters: dict[str, Any] = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "最多列几条，默认 10，最多 50。",
                    "default": 10,
                }
            },
            "required": [],
        }
    )

    async def call(
        self, context: ContextWrapper[AstrAgentContext], limit: int = 10
    ) -> ToolExecResult:
        event, error = await _gate(self.config_manager, self.context, context)
        if error:
            return _json_result({"ok": False, **error})
        with adapter_access(self.config_manager, event):
            try:
                ops = await self.memory_engine.list_memory_ops(int(limit or 10))
            except Exception as exc:
                return _json_result({"ok": False, "error": str(exc)})
        return _json_result(
            {
                "ok": True,
                "count": len(ops),
                "operations": [
                    {
                        "op_id": op["id"],
                        "type": op["op_type"],
                        "status": op["status"],
                        "summary": op["summary"],
                        "created_at": op["created_at"],
                        "report": (op.get("report") or "")[:1200],
                    }
                    for op in ops
                ],
            }
        )


@dataclass
class MemoryOpUndoTool(FunctionTool[AstrAgentContext]):
    """按 op_id 撤销一次记忆操作。"""

    __pydantic_config__ = {"arbitrary_types_allowed": True}

    context: Any = None
    config_manager: Any = None
    memory_engine: Any = None

    name: str = "memory_op_undo"
    description: str = (
        "按 op_id 撤销一次做梦/批量归档/批量更新/dedup 合并，恢复原档和索引。"
        "只能撤销最近的操作；如果新记忆已被后续修改，会拒绝直接删除并给出错误。"
        "只有管理员私聊可用。"
    )
    parameters: dict[str, Any] = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "op_id": {
                    "type": "integer",
                    "description": "memory_op_list 或 dream_preview 返回的 op_id。",
                }
            },
            "required": ["op_id"],
        }
    )

    async def call(
        self, context: ContextWrapper[AstrAgentContext], op_id: int
    ) -> ToolExecResult:
        event, error = await _gate(self.config_manager, self.context, context)
        if error:
            return _json_result({"ok": False, **error})
        with adapter_access(self.config_manager, event):
            try:
                result = await self.memory_engine.undo_memory_op(int(op_id))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("[MemoryOpUndo] 撤销失败: %s", exc, exc_info=True)
                return _json_result({"ok": False, "error": str(exc)})
        return _json_result(result)
