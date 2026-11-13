"""供 Agent 按 id 深读一条长期记忆的完整档案。

与 browse_long_term_memory 配套：浏览列表只给预览，这里给全文、
元数据、修改历史和（可选的）原始聊天证据。
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


def _parse_metadata(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except (TypeError, ValueError):
            return {}
    return {}


@dataclass
class MemoryArchiveReadTool(FunctionTool[AstrAgentContext]):
    """按 id 读完整记忆档案。"""

    __pydantic_config__ = {"arbitrary_types_allowed": True}

    context: Any = None
    config_manager: Any = None
    memory_engine: Any = None

    name: str = "read_long_term_memory"
    description: str = (
        "按 memory_id 读一条长期记忆的完整内容（browse_long_term_memory 返回的 id）。"
        "用于确认某条记忆到底怎么写的、查看主题/关键事实/重要性和修改历史。"
        "需要聊天原文证据时把 include_source 设为 true，但原文可能很长，按需再用。"
        "不要编造 id；只读主人私聊可用的档案。"
    )
    parameters: dict[str, Any] = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "memory_id": {
                    "type": "integer",
                    "description": "记忆 id（browse 或 recall 工具返回的整数 id）。",
                },
                "include_source": {
                    "type": "boolean",
                    "description": "是否附带原始聊天消息证据，默认 false。",
                    "default": False,
                },
            },
            "required": ["memory_id"],
        }
    )

    async def call(
        self,
        context: ContextWrapper[AstrAgentContext],
        memory_id: int,
        include_source: bool = False,
    ) -> ToolExecResult:
        if self.context is None or self.config_manager is None or self.memory_engine is None:
            return _json_result({"ok": False, "error": "memory read tool is not initialized"})

        event, _session_id, _persona_id, access_error = await resolve_tool_access(
            self.config_manager, self.context, context
        )
        with adapter_access(self.config_manager, event):
            if access_error is not None:
                return _json_result({"ok": False, **access_error})

            is_admin = getattr(event, "is_admin", None)
            try:
                admin_allowed = bool(is_admin()) if callable(is_admin) else False
            except Exception:
                admin_allowed = False
            if not admin_allowed:
                # WebChat 私聊即主人（与手机运行时 WebChatMessageEvent 闸门一致）
                try:
                    admin_allowed = (
                        event.get_platform_name() == "webchat"
                        and event.is_private_chat()
                    )
                except Exception:
                    admin_allowed = False
            if not admin_allowed:
                return _json_result({"ok": False, "error": "admin permission required"})
            try:
                if not event.is_private_chat():
                    return _json_result({"ok": False, "error": "memory archive is private-chat only"})
            except Exception:
                pass

            try:
                memory_id = int(memory_id)
            except (TypeError, ValueError):
                return _json_result({"ok": False, "error": "memory_id must be an integer"})

            try:
                record = await self.memory_engine.get_memory_record(memory_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("[MemoryArchiveRead] 读取失败: %s", exc, exc_info=True)
                return _json_result({"ok": False, "error": "read failed"})

            if not record:
                return _json_result({"ok": False, "error": "memory not found", "memory_id": memory_id})

            md = _parse_metadata(record.get("metadata"))
            text = str(record.get("text") or "")
            payload: dict[str, Any] = {
                "ok": True,
                "id": record.get("id"),
                "text": text[:6000] + ("…" if len(text) > 6000 else ""),
                "created_at": record.get("created_at"),
                "updated_at": record.get("updated_at"),
                "date": md.get("source_time_label") or (md.get("time_tags") or [None])[0],
                "topics": md.get("topics") or [],
                "key_facts": md.get("key_facts") or [],
                "importance": md.get("importance"),
                "status": md.get("status", "active"),
                "session_id": md.get("session_id"),
                "memory_origin": md.get("memory_origin"),
                "canonical_summary": md.get("canonical_summary"),
                "update_history": md.get("update_history") or [],
            }

            if include_source:
                try:
                    get_source = getattr(self.memory_engine, "get_memory_source", None)
                    if callable(get_source):
                        source = await get_source(memory_id)
                        if isinstance(source, list):
                            payload["source_messages"] = [
                                {
                                    "role": item.get("role"),
                                    "sender": item.get("sender_name") or item.get("sender_id"),
                                    "timestamp": item.get("timestamp"),
                                    "content": str(item.get("content") or "")[:800],
                                }
                                for item in source[:20]
                            ]
                except Exception as exc:
                    logger.debug("[MemoryArchiveRead] 读原始消息失败: %s", exc)

            return _json_result(payload)
