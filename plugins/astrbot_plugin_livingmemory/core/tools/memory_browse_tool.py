"""供 Agent 翻阅长期记忆档案的分页浏览工具。

这不是语义检索，而是「翻档案」：按时间或重要性分页列出记忆，
让她能先看到有哪些页、哪一页写了什么，再决定深读、改写或归档。
仅管理员在私聊中可用，避免群聊陌生人翻主人的记忆。
"""

from ..memory_scope import adapter_access
import asyncio
import json
import math
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


def _metadata_of(item: dict[str, Any]) -> dict[str, Any]:
    raw = item.get("metadata")
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
class MemoryBrowseTool(FunctionTool[AstrAgentContext]):
    """分页翻阅长期记忆档案（不触发语义检索）。"""

    __pydantic_config__ = {"arbitrary_types_allowed": True}

    context: Any = None
    config_manager: Any = None
    memory_engine: Any = None

    name: str = "browse_long_term_memory"
    description: str = (
        "翻阅自己的长期记忆档案，像一页一页翻笔记本。用于：突然想自己查某件事、"
        "确认某条记忆写成了什么样、找一条只记得大概日期的记录、看看最近都记了什么。"
        "返回分页列表（记忆 id、日期、主题、预览、状态）。要读全文用 read_long_term_memory；"
        "要改错用 edit_long_term_memory；要删掉写错的用 forget_long_term_memory。"
        "只有主人私聊里能用，不要主动在群里翻。"
    )
    parameters: dict[str, Any] = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "page": {
                    "type": "integer",
                    "description": "第几页，从 1 开始。先看第 1 页，再按需要往后翻。",
                    "default": 1,
                },
                "page_size": {
                    "type": "integer",
                    "description": "每页多少条，默认 8，最多 20。",
                    "default": 8,
                },
                "keyword": {
                    "type": "string",
                    "description": "可选：只翻正文/类型里包含这个词的记忆（按字面匹配，不是语义搜索）。",
                    "default": "",
                },
                "status": {
                    "type": "string",
                    "enum": ["active", "archived", "all"],
                    "description": "看哪种状态：active 正常、archived 已归档、all 全部。默认 active。",
                    "default": "active",
                },
                "sort": {
                    "type": "string",
                    "enum": ["latest", "oldest", "importance", "event_asc", "event_desc"],
                    "description": "排序：latest 最新在前、oldest 最早在前、importance 重要性在前、event_asc/event_desc 按事件发生日期。",
                    "default": "latest",
                },
                "date_start": {
                    "type": "string",
                    "description": "可选：只看这个日期之后的经历，格式 YYYY-MM-DD。",
                    "default": "",
                },
                "date_end": {
                    "type": "string",
                    "description": "可选：只看这个日期之前的经历，格式 YYYY-MM-DD。",
                    "default": "",
                },
            },
            "required": [],
        }
    )

    async def call(
        self,
        context: ContextWrapper[AstrAgentContext],
        page: int = 1,
        page_size: int = 8,
        keyword: str = "",
        status: str = "active",
        sort: str = "latest",
        date_start: str = "",
        date_end: str = "",
    ) -> ToolExecResult:
        if self.context is None or self.config_manager is None or self.memory_engine is None:
            return _json_result({"ok": False, "error": "memory browse tool is not initialized"})

        event, recall_session_id, _persona_id, access_error = await resolve_tool_access(
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
                page = max(1, int(page or 1))
            except (TypeError, ValueError):
                page = 1
            try:
                page_size = max(1, min(20, int(page_size or 8)))
            except (TypeError, ValueError):
                page_size = 8

            status = str(status or "active").strip().lower()
            if status not in {"active", "archived", "all"}:
                status = "active"

            sort_key = {
                "latest": "created_desc",
                "oldest": "created_asc",
                "importance": "importance_desc",
                "event_asc": "event_asc",
                "event_desc": "event_desc",
            }.get(str(sort or "latest").strip().lower(), "created_desc")

            try:
                result = await self.memory_engine.list_memories_page(
                    session_id=recall_session_id,
                    keyword=str(keyword or "").strip(),
                    status=status,
                    date_start=str(date_start or "").strip() or None,
                    date_end=str(date_end or "").strip() or None,
                    sort_key=sort_key,
                    page=page,
                    page_size=page_size,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("[MemoryBrowse] 翻阅记忆失败: %s", exc, exc_info=True)
                return _json_result({"ok": False, "error": "browse failed"})

            items = []
            for row in result.get("items") or []:
                md = _metadata_of(row)
                text = str(row.get("text") or "")
                preview = text[:180] + ("…" if len(text) > 180 else "")
                items.append(
                    {
                        "id": row.get("id"),
                        "date": (
                            str(md.get("event_start") or "")[:10]
                            or md.get("source_time_label")
                            or (md.get("time_tags") or [None])[0]
                            or row.get("created_at")
                        ),
                        "event_start": md.get("event_start"),
                        "event_end": md.get("event_end"),
                        "created_at": row.get("created_at"),
                        "updated_at": row.get("updated_at"),
                        "topics": md.get("topics") or [],
                        "key_facts": (md.get("key_facts") or [])[:3],
                        "importance": md.get("importance"),
                        "status": md.get("status", "active"),
                        "source": md.get("memory_origin") or md.get("adapter_id") or "",
                        "preview": preview,
                    }
                )

            total = int(result.get("total") or 0)
            return _json_result(
                {
                    "ok": True,
                    "page": result.get("page", page),
                    "page_size": result.get("page_size", page_size),
                    "total": total,
                    "total_pages": max(1, math.ceil(total / page_size)) if total else 0,
                    "has_more": bool(result.get("has_more")),
                    "sort": sort,
                    "status": status,
                    "keyword": str(keyword or "").strip(),
                    "items": items,
                    "hint": "看完整内容用 read_long_term_memory(memory_id)；"
                    "写错了用 edit_long_term_memory；要删用 forget_long_term_memory。",
                }
            )
