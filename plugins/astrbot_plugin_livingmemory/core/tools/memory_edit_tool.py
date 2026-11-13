"""供 Agent 改写一条长期记忆档案的工具（管理员、私聊）。

写入方式与 WebUI 记忆页一致：内容/主题/关键事实会触发完整重建
（向量、BM25、原子、图数据一起更新），旧记忆会被替换成新的 id。
"""

from ..memory_scope import adapter_access
import asyncio
import json
import time
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


def _norm_list(value: Any, limit: int = 8) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    out = []
    for item in value:
        text = str(item).strip()
        if text and text not in out:
            out.append(text[:200])
        if len(out) >= limit:
            break
    return out


def _history_value(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


@dataclass
class MemoryEditTool(FunctionTool[AstrAgentContext]):
    """改写一条长期记忆（内容 / 主题 / 关键事实 / 重要性）。"""

    __pydantic_config__ = {"arbitrary_types_allowed": True}

    context: Any = None
    config_manager: Any = None
    memory_engine: Any = None

    name: str = "edit_long_term_memory"
    description: str = (
        "改写一条写错的长期记忆。先用 browse_long_term_memory 或 recall_long_term_memory "
        "找到准确 memory_id，再用本工具修正。content 要传改写后的完整新内容，不是只传改动的几个字；"
        "也可以顺手更新 topics / key_facts / importance。修改会重建索引并生成新的 memory_id，"
        "旧 id 会失效。只有主人私聊可用，改动会留修改历史。"
    )
    parameters: dict[str, Any] = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "memory_id": {
                    "type": "integer",
                    "description": "要改写的记忆 id（browse/recall 返回的整数 id）。",
                },
                "content": {
                    "type": "string",
                    "description": "改写后的完整记忆正文；不传则保持原文。",
                    "default": "",
                },
                "topics": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "可选：新的主题标签（最多 8 个）。",
                },
                "key_facts": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "可选：新的关键事实（最多 8 条）。",
                },
                "importance": {
                    "type": "number",
                    "description": "可选：重要性 0.0~1.0；不传保持原值。",
                },
                "reason": {
                    "type": "string",
                    "description": "可选：为什么改（会写进修改历史）。",
                    "default": "",
                },
            },
            "required": ["memory_id"],
        }
    )

    async def call(
        self,
        context: ContextWrapper[AstrAgentContext],
        memory_id: int,
        content: str = "",
        topics: list[str] | None = None,
        key_facts: list[str] | None = None,
        importance: float | None = None,
        reason: str = "",
    ) -> ToolExecResult:
        if self.context is None or self.config_manager is None or self.memory_engine is None:
            return _json_result({"ok": False, "error": "memory edit tool is not initialized"})

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
                logger.warning("[MemoryEdit] 读取失败: %s", exc, exc_info=True)
                return _json_result({"ok": False, "error": "read failed"})
            if not record:
                return _json_result({"ok": False, "error": "memory not found", "memory_id": memory_id})

            md = _parse_metadata(record.get("metadata"))
            old_content = str(record.get("text") or "")
            new_content = (content or "").strip() or old_content
            if not new_content:
                return _json_result({"ok": False, "error": "content cannot be empty"})
            if len(new_content) > 6000:
                return _json_result({"ok": False, "error": "content too long; keep it under 6000 chars"})

            old_topics = _norm_list(md.get("topics"))
            new_topics = _norm_list(topics) if topics is not None else old_topics
            old_facts = _norm_list(md.get("key_facts"))
            new_facts = _norm_list(key_facts) if key_facts is not None else old_facts
            try:
                old_importance = float(md.get("importance", 0.5))
            except (TypeError, ValueError):
                old_importance = 0.5
            if importance is None:
                new_importance = old_importance
            else:
                try:
                    new_importance = max(0.0, min(1.0, float(importance)))
                except (TypeError, ValueError):
                    return _json_result({"ok": False, "error": "importance must be a number"})

            changed: list[str] = []
            if new_content != old_content:
                changed.append("content")
            if new_topics != old_topics:
                changed.append("topics")
            if new_facts != old_facts:
                changed.append("key_facts")
            if abs(new_importance - old_importance) > 1e-9:
                changed.append("importance")
            if not changed:
                return _json_result(
                    {
                        "ok": True,
                        "updated": False,
                        "memory_id": memory_id,
                        "message": "没有检测到修改",
                    }
                )

            now = time.time()
            history = md.get("update_history")
            history = [item for item in history[-19:] if isinstance(item, dict)] if isinstance(history, list) else []
            snapshot = {
                "content": (old_content, new_content),
                "topics": (old_topics, new_topics),
                "key_facts": (old_facts, new_facts),
                "importance": (old_importance, new_importance),
            }
            for name in changed:
                old_value, new_value = snapshot[name]
                history.append(
                    {
                        "timestamp": now,
                        "field": name,
                        "old_value": _history_value(old_value),
                        "new_value": _history_value(new_value),
                        "reason": str(reason or "").strip(),
                    }
                )

            new_md = md.copy()
            new_md["topics"] = new_topics
            new_md["key_facts"] = new_facts
            new_md["importance"] = new_importance
            new_md["update_history"] = history
            new_md["updated_at"] = now
            if "content" in changed:
                new_md["previous_content"] = old_content[:100]
                new_md["canonical_summary"] = new_content
                new_md["persona_summary"] = new_content
            if str(reason or "").strip():
                new_md["update_reason"] = str(reason).strip()

            try:
                new_memory_id = await self.memory_engine.replace_memory(
                    memory_id,
                    content=new_content,
                    importance=new_importance,
                    metadata=new_md,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("[MemoryEdit] 改写失败: %s", exc, exc_info=True)
                return _json_result({"ok": False, "error": f"edit failed: {exc}"})

            return _json_result(
                {
                    "ok": True,
                    "updated": True,
                    "old_memory_id": memory_id,
                    "new_memory_id": new_memory_id,
                    "changed": changed,
                    "reason": str(reason or "").strip(),
                    "message": "已改写并重建索引；后续请用新的 memory_id。",
                }
            )
