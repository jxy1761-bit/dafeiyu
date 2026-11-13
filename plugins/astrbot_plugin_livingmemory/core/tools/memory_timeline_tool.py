"""按时间线回忆共同经历的长期记忆工具。"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import field
from typing import Any

from pydantic.dataclasses import dataclass

from astrbot.api import logger
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import FunctionTool, ToolExecResult
from astrbot.core.astr_agent_context import AstrAgentContext

from ..memory_scope import adapter_access
from .agentic_recall_common import resolve_tool_access


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


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


def _participants(metadata: dict[str, Any]) -> list[str]:
    identities = metadata.get("participant_identities")
    if isinstance(identities, list) and identities:
        names = []
        for item in identities:
            if not isinstance(item, dict):
                continue
            name = str(item.get("display_name") or item.get("sender_id") or "").strip()
            if item.get("is_bot"):
                name = "我"
            if name and name not in names:
                names.append(name)
        if names:
            return names
    raw = metadata.get("participants")
    return [str(item) for item in raw if item] if isinstance(raw, list) else []


def _emotion_summary(metadata: dict[str, Any]) -> dict[str, Any] | None:
    emotion = metadata.get("emotion")
    if not isinstance(emotion, dict):
        return None
    labels = emotion.get("labels")
    if not isinstance(labels, list) or not labels:
        return None
    return {
        "labels": labels,
        "explicit": bool(emotion.get("explicit")),
        "inferred": bool(emotion.get("inferred", not emotion.get("explicit"))),
        "evidence": str(emotion.get("evidence") or ""),
    }


@dataclass
class MemoryTimelineTool(FunctionTool[AstrAgentContext]):
    """按日期范围列出那段日子里发生过的经历。"""

    __pydantic_config__ = {"arbitrary_types_allowed": True}

    context: Any = None
    config_manager: Any = None
    memory_engine: Any = None

    name: str = "recall_episodes_by_time"
    description: str = (
        "按时间线回忆共同经历。用于主人问“上周发生了什么”“那天我们聊了什么”"
        "“最近几天有什么重要的事”之类的问题。先用日期范围缩小范围，"
        "再按人物或主题过滤；返回每条经历的事件日期、参与者、主题、关键事实、"
        "情绪和置信度。需要原文细节时再用 read_long_term_memory 或 "
        "recall_long_term_memory。把“上周/前天/上个月”换算成 YYYY-MM-DD 再调用。"
    )
    parameters: dict[str, Any] = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "date_start": {
                    "type": "string",
                    "description": "起始日期 YYYY-MM-DD（含当天）。不填则不设下限。",
                    "default": "",
                },
                "date_end": {
                    "type": "string",
                    "description": "结束日期 YYYY-MM-DD（含当天）。不填则不设上限。",
                    "default": "",
                },
                "person": {
                    "type": "string",
                    "description": "可选：只看和某个人有关的经历，填昵称的一部分即可。",
                    "default": "",
                },
                "topic": {
                    "type": "string",
                    "description": "可选：只看包含某个主题的经历，例如“跑步”“语音”。",
                    "default": "",
                },
                "limit": {
                    "type": "integer",
                    "description": "最多返回多少条经历，默认 8，最多 20。",
                    "default": 8,
                },
                "newest_first": {
                    "type": "boolean",
                    "description": "true 从新到旧，false 从旧到新；默认从旧到新，便于顺着时间讲。",
                    "default": False,
                },
            },
            "required": [],
        }
    )

    async def call(
        self,
        context: ContextWrapper[AstrAgentContext],
        date_start: str = "",
        date_end: str = "",
        person: str = "",
        topic: str = "",
        limit: int = 8,
        newest_first: bool = False,
    ) -> ToolExecResult:
        if self.context is None or self.config_manager is None or self.memory_engine is None:
            return _json_result({"ok": False, "error": "memory timeline tool is not initialized"})

        start = str(date_start or "").strip()
        end = str(date_end or "").strip()
        for value in (start, end):
            if value and not _DATE_RE.match(value):
                return _json_result(
                    {"ok": False, "error": "date must be YYYY-MM-DD", "value": value}
                )
        if start and end and start > end:
            start, end = end, start

        try:
            limit = max(1, min(20, int(limit or 8)))
        except (TypeError, ValueError):
            limit = 8

        event, recall_session_id, _persona_id, access_error = await resolve_tool_access(
            self.config_manager, self.context, context
        )
        with adapter_access(self.config_manager, event):
            if access_error is not None:
                return _json_result({"ok": False, **access_error})

            try:
                result = await self.memory_engine.list_memories_page(
                    session_id=recall_session_id,
                    status="active",
                    date_start=start or None,
                    date_end=end or None,
                    participant=str(person or "").strip() or None,
                    topic=str(topic or "").strip() or None,
                    sort_key="event_desc" if newest_first else "event_asc",
                    page=1,
                    page_size=limit,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("[MemoryTimeline] 查询经历失败: %s", exc, exc_info=True)
                return _json_result({"ok": False, "error": "timeline query failed"})

            episodes = []
            for row in result.get("items") or []:
                metadata = _metadata_of(row)
                text = str(metadata.get("persona_summary") or row.get("text") or "").strip()
                episodes.append(
                    {
                        "id": row.get("id"),
                        "date_start": str(metadata.get("event_start") or "")[:19],
                        "date_end": str(metadata.get("event_end") or "")[:19],
                        "participants": _participants(metadata),
                        "topics": (metadata.get("topics") or [])[:5],
                        "key_facts": (metadata.get("key_facts") or [])[:4],
                        "emotion": _emotion_summary(metadata),
                        "importance": metadata.get("importance"),
                        "confidence": metadata.get("confidence"),
                        "summary": text[:240] + ("…" if len(text) > 240 else ""),
                    }
                )

            return _json_result(
                {
                    "ok": True,
                    "count": len(episodes),
                    "date_start": start or None,
                    "date_end": end or None,
                    "person": str(person or "").strip() or None,
                    "topic": str(topic or "").strip() or None,
                    "episodes": episodes,
                    "hint": (
                        "没有结果时先放宽日期或去掉人物/主题过滤；"
                        "要读某条完整内容用 read_long_term_memory(memory_id)。"
                    ),
                }
            )
