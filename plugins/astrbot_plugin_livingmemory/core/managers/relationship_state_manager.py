"""关系连续性状态：从近期经历与计划中提炼长期主题、未完成线索和近期重要事件。

只做可解释的聚合，不产出“亲密度分数”。状态落盘为 JSON，注入时作为背景参考。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

from astrbot.api import logger


_GENERIC_TOPICS = {
    "日常闲聊",
    "日常拌嘴",
    "闲聊",
    "对话",
    "记忆检查",
    "功能测试",
    "约定",
}

_PROMISE_MARKERS = ("答应", "约定", "欠", "下次", "还没", "没定", "待定")
_FUTURE_MARKERS = (
    "明天",
    "后天",
    "下次",
    "周末",
    "下周",
    "下个月",
    "以后",
    "还没",
    "待定",
    "准备",
    "计划",
    "打算",
    "约定",
    "答应",
    "欠",
)
_DONE_MARKERS = ("已经", "已完成", "完成了", "生效了", "改好了", "修好了", "搞定", "结束了")
_SENSITIVE_MARKERS = ("不要提", "别记", "不想记", "不许问", "别问", "隐私", "不要告诉")
_HEALTH_SNAPSHOT_MARKERS = ("步、", "心率", "血氧", "压力", "身体数据")
_DATE_PREFIX_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[^\d]{0,3}")
_FULL_DATE_RES = (
    re.compile(r"(20\d{2})-(\d{1,2})-(\d{1,2})"),
    re.compile(r"(20\d{2})年(\d{1,2})月(\d{1,2})日"),
    re.compile(r"今年(\d{1,2})月(\d{1,2})日"),
    re.compile(r"(?<!\d)(\d{1,2})月(\d{1,2})日"),
)


def _explicit_dates(text: str, now: float) -> list[datetime]:
    """从中文/ISO 日期里取出可比对的日期；无年份按今年解释。"""
    current_year = datetime.fromtimestamp(now).year
    dates: list[datetime] = []
    for index, pattern in enumerate(_FULL_DATE_RES):
        for match in pattern.finditer(text):
            groups = match.groups()
            try:
                if index == 0:
                    year, month, day = int(groups[0]), int(groups[1]), int(groups[2])
                elif index == 1:
                    year, month, day = int(groups[0]), int(groups[1]), int(groups[2])
                elif index == 2:
                    year, month, day = current_year, int(groups[0]), int(groups[1])
                else:
                    year, month, day = current_year, int(groups[0]), int(groups[1])
                dates.append(datetime(year, month, day))
            except (TypeError, ValueError):
                continue
        if dates:
            return dates
    return dates


def _looks_actionable_loop(
    text: str, event_time: Any, now: float, created_at: float | None = None
) -> bool:
    """未完成线索必须仍然指向未来，或明确是“还没做”的约定。"""
    content = str(text or "").strip()
    if not content:
        return False
    if any(marker in content for marker in _DONE_MARKERS):
        return False
    if any(marker in content for marker in _HEALTH_SNAPSHOT_MARKERS):
        return False

    today = datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0)
    dates = _explicit_dates(content, now)
    if dates and any(value < today for value in dates):
        return False
    has_future_date = any(value >= today for value in dates)
    has_future_marker = any(marker in content for marker in _FUTURE_MARKERS)

    if not has_future_date and not has_future_marker:
        return False

    if event_time is not None:
        try:
            event_value = float(event_time)
        except (TypeError, ValueError):
            event_value = 0.0
        if event_value < now - 86400 and not has_future_date:
            return False

    # 没有明确未来日期、只靠“明天/下次”这类词的线索，必须足够新且重要。
    if not has_future_date:
        if created_at is not None:
            try:
                if float(created_at) < now - 14 * 86400:
                    return False
            except (TypeError, ValueError):
                pass
    return True



def _normalize_text(value: Any, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:limit] + ("…" if len(text) > limit else "")


def _event_date(metadata: dict[str, Any]) -> str:
    raw = str(metadata.get("event_start") or metadata.get("source_time_start") or "")
    if len(raw) >= 10 and re.match(r"^\d{4}-\d{2}-\d{2}", raw):
        return raw[:10]
    try:
        return datetime.fromtimestamp(float(metadata.get("create_time"))).strftime(
            "%Y-%m-%d"
        )
    except (TypeError, ValueError, OSError):
        return ""


def _importance(metadata: dict[str, Any]) -> float:
    try:
        return float(metadata.get("importance", 0.5))
    except (TypeError, ValueError):
        return 0.5


def _explicit_emotion(metadata: dict[str, Any]) -> list[str]:
    emotion = metadata.get("emotion")
    if not isinstance(emotion, dict) or not emotion.get("explicit"):
        return []
    labels = emotion.get("labels")
    return [str(item) for item in labels if item][:3] if isinstance(labels, list) else []


class RelationshipStateManager:
    """Build and cache a compact relationship-continuity state."""

    def __init__(
        self,
        memory_engine: Any,
        data_dir: str | Path,
        config: dict[str, Any] | None = None,
    ):
        self.memory_engine = memory_engine
        self.data_dir = Path(data_dir)
        self.path = self.data_dir / "relationship_state.json"
        self.config = config or {}
        self.enabled = bool(self.config.get("relationship_state_enabled", True))
        self.refresh_hours = float(
            self.config.get("relationship_state_refresh_hours", 6.0)
        )
        self.window_days = int(self.config.get("relationship_state_window_days", 30))
        self._states: dict[str, dict[str, Any]] = {}
        self._running = False
        self._task: asyncio.Task | None = None
        self._refresh_task: asyncio.Task | None = None
        self._last_refresh = 0.0

    async def initialize(self) -> None:
        if not self.enabled:
            return
        self._load()

    async def start(self) -> None:
        if not self.enabled or self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._refresh_loop())

    async def stop(self) -> None:
        self._running = False
        for task in (self._task, self._refresh_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._task = None
        self._refresh_task = None

    def schedule_refresh(self, delay: float = 10.0) -> None:
        """Schedule a debounced refresh without blocking the caller."""
        if not self.enabled:
            return
        if self._refresh_task and not self._refresh_task.done():
            return
        if time.time() - self._last_refresh < 600.0:
            return
        try:
            self._refresh_task = asyncio.create_task(self._delayed_refresh(delay))
        except RuntimeError:
            self._refresh_task = None

    async def _delayed_refresh(self, delay: float) -> None:
        try:
            await asyncio.sleep(max(0.0, delay))
            await self.refresh()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.error("[RelationshipState] 刷新失败", exc_info=True)

    async def _refresh_loop(self) -> None:
        while self._running:
            try:
                await self.refresh()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.error("[RelationshipState] 定时刷新失败", exc_info=True)
            await asyncio.sleep(max(1.0, self.refresh_hours) * 3600.0)

    async def refresh(self) -> dict[str, Any]:
        if not self.enabled:
            return {}
        connection = getattr(self.memory_engine, "db_connection", None)
        if connection is None:
            return {}
        try:
            states = await self._build_states(connection)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.error("[RelationshipState] 构建状态失败", exc_info=True)
            return {}

        self._states = states
        self._last_refresh = time.time()
        self._write()
        return states

    async def _build_states(self, connection: Any) -> dict[str, dict[str, Any]]:
        now = time.time()
        cutoff = now - self.window_days * 86400
        recent_cutoff = now - 14 * 86400

        cursor = await connection.execute(
            "SELECT id, text, metadata FROM documents "
            "WHERE COALESCE(json_extract(metadata, '$.status'), 'active') = 'active' "
            "ORDER BY CAST(json_extract(metadata, '$.create_time') AS REAL) DESC "
            "LIMIT 500"
        )
        rows = await cursor.fetchall()

        themes: dict[str, Counter] = defaultdict(Counter)
        theme_last: dict[str, dict[str, str]] = defaultdict(dict)
        recent: dict[str, list[dict[str, Any]]] = defaultdict(list)
        sensitive: dict[str, list[dict[str, Any]]] = defaultdict(list)
        promises: dict[str, list[dict[str, Any]]] = defaultdict(list)
        seen_personas: set[str] = set()

        for row in rows:
            try:
                metadata = json.loads(row["metadata"] or "{}")
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(metadata, dict):
                continue
            persona = str(metadata.get("persona_id") or "default")
            seen_personas.add(persona)

            create_time = float(metadata.get("create_time") or 0)
            if create_time and create_time < cutoff:
                continue
            date = _event_date(metadata)
            topics = metadata.get("topics")
            if isinstance(topics, list):
                for topic in topics:
                    name = str(topic or "").strip()
                    if not name or name in _GENERIC_TOPICS or len(name) > 20:
                        continue
                    themes[persona][name] += 1
                    if date and date > theme_last[persona].get(name, ""):
                        theme_last[persona][name] = date

            facts = metadata.get("key_facts")
            if isinstance(facts, list):
                for fact in facts:
                    text = str(fact or "").strip()
                    if not text:
                        continue
                    if any(marker in text for marker in _SENSITIVE_MARKERS):
                        sensitive[persona].append(
                            {
                                "text": _normalize_text(text, 60),
                                "date": date,
                                "memory_id": row["id"],
                            }
                        )
                        continue
                    if any(marker in text for marker in _PROMISE_MARKERS) and _looks_actionable_loop(
                        text, None, now, create_time or None
                    ):
                        if not _explicit_dates(text, now) and _importance(metadata) < 0.7:
                            continue
                        promises[persona].append(
                            {
                                "text": _normalize_text(text, 70),
                                "date": date,
                                "memory_id": row["id"],
                                "importance": _importance(metadata),
                            }
                        )

            if create_time >= recent_cutoff and _importance(metadata) >= 0.75:
                summary = str(
                    metadata.get("persona_summary")
                    or metadata.get("canonical_summary")
                    or row["text"]
                ).strip()
                summary = _DATE_PREFIX_RE.sub("", summary)
                recent[persona].append(
                    {
                        "text": _normalize_text(summary, 70),
                        "date": date,
                        "memory_id": row["id"],
                        "importance": _importance(metadata),
                        "emotion": _explicit_emotion(metadata),
                    }
                )

        loops = await self._planned_atoms(connection, now)

        states: dict[str, dict[str, Any]] = {}
        for persona in seen_personas:
            loop_items = self._dedupe_items(
                [*loops.get(persona, []), *promises.get(persona, [])], 5
            )
            states[persona] = {
                "persona_id": persona,
                "updated_at": now,
                "window_days": self.window_days,
                "themes": [
                    {
                        "name": name,
                        "count": count,
                        "last_seen": theme_last[persona].get(name, ""),
                    }
                    for name, count in themes[persona].most_common(6)
                ],
                "open_loops": loop_items,
                "recent_events": sorted(
                    self._dedupe_items(recent[persona], 3),
                    key=lambda item: (item["importance"], item["date"]),
                    reverse=True,
                )[:3],
                "sensitive_topics": self._dedupe_items(sensitive[persona], 3),
            }
        return states

    async def _planned_atoms(
        self, connection: Any, now: float
    ) -> dict[str, list[dict[str, Any]]]:
        try:
            cursor = await connection.execute(
                "SELECT content, importance, event_time, persona_id, parent_memory_id, "
                "created_at FROM memory_atoms "
                "WHERE status = 'active' AND atom_type = 'planned' AND expires_at > ? "
                "ORDER BY importance DESC, expires_at ASC LIMIT 100",
                (now,),
            )
            rows = await cursor.fetchall()
        except Exception:
            return {}

        result: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            event_time = row["event_time"]
            created_at = row["created_at"]
            if not _looks_actionable_loop(row["content"], event_time, now, created_at):
                continue
            if not _explicit_dates(str(row["content"]), now) and (
                float(row["importance"] or 0.5) < 0.7
            ):
                continue
            persona = str(row["persona_id"] or "default")
            date = ""
            if event_time:
                try:
                    date = datetime.fromtimestamp(float(event_time)).strftime("%Y-%m-%d")
                except (TypeError, ValueError, OSError):
                    date = ""
            result[persona].append(
                {
                    "text": _normalize_text(row["content"], 70),
                    "date": date,
                    "memory_id": row["parent_memory_id"],
                    "importance": float(row["importance"] or 0.5),
                    "kind": "plan",
                }
            )
        return result

    @staticmethod
    def _dedupe_items(items: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
        seen: set[str] = set()
        result: list[dict[str, Any]] = []
        for item in items:
            key = re.sub(r"\W+", "", str(item.get("text") or ""))[:24]
            if not key or key in seen:
                continue
            seen.add(key)
            result.append(item)
            if len(result) >= limit:
                break
        return result

    def get_state(self, persona_id: str | None = None) -> dict[str, Any] | None:
        if not self._states:
            return None
        return self._states.get(str(persona_id or "default")) or self._states.get(
            "default"
        )

    def render_for_injection(
        self, persona_id: str | None = None, max_chars: int = 420
    ) -> str:
        if not self.enabled:
            return ""
        state = self.get_state(persona_id)
        if not state:
            return ""

        lines: list[str] = []
        themes = state.get("themes") or []
        if themes:
            lines.append(
                "近期主题: "
                + "、".join(f"{item['name']}×{item['count']}" for item in themes[:5])
            )

        loops = state.get("open_loops") or []
        if loops:
            parts = []
            for item in loops[:3]:
                prefix = f"[{item['date']}] " if item.get("date") else ""
                parts.append(prefix + str(item.get("text") or ""))
            lines.append("未完成线索: " + "；".join(parts))

        events = state.get("recent_events") or []
        if events:
            item = events[0]
            prefix = f"[{item['date']}] " if item.get("date") else ""
            emotion = f"（{ '、'.join(item['emotion']) }）" if item.get("emotion") else ""
            lines.append(f"近期重要: {prefix}{item.get('text')}{emotion}")

        sensitive = state.get("sensitive_topics") or []
        if sensitive and self.config.get("relationship_state_inject_sensitive", False):
            lines.append(
                "谨慎提及: " + "；".join(str(item.get("text")) for item in sensitive[:2])
            )

        if not lines:
            return ""

        header = "--- RELATIONSHIP CONTINUITY（背景参考，不是当前事实）---"
        footer = (
            "只在与当前话题相关时自然提起；不要假定主人现在仍是同样的心情。\n"
            "--- END RELATIONSHIP CONTINUITY ---"
        )
        body: list[str] = []
        budget = max_chars - len(header) - len(footer) - 2
        for line in lines:
            if len(line) + 1 > budget:
                continue
            body.append(line)
            budget -= len(line) + 1
        if not body:
            return ""
        return header + "\n" + "\n".join(body) + "\n" + footer

    def _load(self) -> None:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            states = payload.get("personas") if isinstance(payload, dict) else None
            if isinstance(states, dict):
                self._states = {
                    str(key): value
                    for key, value in states.items()
                    if isinstance(value, dict)
                }
                self._last_refresh = float(payload.get("updated_at") or 0)
        except FileNotFoundError:
            pass
        except Exception:
            logger.warning("[RelationshipState] 读取状态失败，将重建", exc_info=True)

    def _write(self) -> None:
        payload = {
            "version": 1,
            "updated_at": self._last_refresh,
            "personas": self._states,
        }
        tmp = self.path.with_suffix(".json.tmp")
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
            )
            os.replace(tmp, self.path)
        except Exception:
            logger.warning("[RelationshipState] 写入状态失败", exc_info=True)


__all__ = ["RelationshipStateManager"]
