"""Phase D：时间线索召回测试。"""

from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timedelta

import aiosqlite

from astrbot_plugin_livingmemory.core.managers.memory_engine import MemoryEngine
from astrbot_plugin_livingmemory.core.retrieval.temporal_cue import (
    has_recall_intent,
    parse_temporal_range,
)


def test_temporal_parser_understands_relative_ranges():
    now = datetime(2026, 11, 13, 12, 0)

    assert parse_temporal_range("今天好累", now) is None
    assert has_recall_intent("今天好累") is False

    start, end, label = parse_temporal_range("上周我们聊了什么", now)
    assert label == "上周"
    assert start == "2026-11-02"
    assert end == "2026-11-08"

    start, end, label = parse_temporal_range("还记得上个月那次跑步吗", now)
    assert label == "上个月"
    assert start == "2026-10-01"
    assert end == "2026-10-31"

    start, end, label = parse_temporal_range("11月11日那天发生了什么", now)
    assert label == "指定日期"
    assert start == end == "2026-11-11"

    start, end, label = parse_temporal_range("去年冬天说过什么", now)
    assert label == "去年"
    assert start == "2025-01-01"
    assert end == "2025-12-31"


def test_temporal_channel_merges_episode_candidates():
    today = date.today()
    monday = today - timedelta(days=today.weekday())
    last_monday = monday - timedelta(weeks=1)
    in_range = last_monday.isoformat()
    out_range = (monday - timedelta(weeks=3)).isoformat()

    async def scenario():
        connection = await aiosqlite.connect(":memory:")
        connection.row_factory = aiosqlite.Row
        await connection.execute(
            "CREATE TABLE documents (id INTEGER PRIMARY KEY, doc_id TEXT, text TEXT, "
            "metadata TEXT, created_at TEXT, updated_at TEXT)"
        )
        rows = [
            (1, in_range, 0.9, "上周一起看了烟花"),
            (2, out_range, 0.9, "三周前的旧事"),
        ]
        for doc_id, event_start, importance, text in rows:
            metadata = {
                "status": "active",
                "persona_id": "大肥鱼",
                "importance": importance,
                "create_time": 1794000000 + doc_id,
                "event_start": f"{event_start}T20:00:00+08:00",
                "topics": ["回忆"],
            }
            await connection.execute(
                "INSERT INTO documents (id, doc_id, text, metadata, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, '2026-11-01 00:00:00', '2026-11-01 00:00:00')",
                (doc_id, f"doc-{doc_id}", text, json.dumps(metadata, ensure_ascii=False)),
            )
        await connection.commit()

        engine = object.__new__(MemoryEngine)
        engine.db_connection = connection
        results = await engine._merge_temporal_candidates(
            [], "上周我们聊了什么", 5, None, "大肥鱼"
        )
        await connection.close()
        return results

    results = asyncio.run(scenario())
    assert len(results) == 1
    assert results[0].content == "上周一起看了烟花"
    assert results[0].score_breakdown["temporal"] == 1.0
    assert results[0].score_breakdown["date_label"] == "上周"
