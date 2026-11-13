"""Phase C：关系连续性状态测试。"""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

import aiosqlite

from astrbot_plugin_livingmemory.core.managers.relationship_state_manager import (
    RelationshipStateManager,
)
from astrbot_plugin_livingmemory.core.models.memory_atom import AtomType, MemoryAtom
from astrbot_plugin_livingmemory.storage.atom_store import AtomStore


def test_relationship_state_aggregates_themes_loops_and_sensitivity(tmp_path):
    db_path = tmp_path / "livingmemory.db"
    now = time.time()

    async def scenario():
        store = AtomStore(str(db_path))
        await store.initialize()
        await store.insert(
            MemoryAtom(
                parent_memory_id=2,
                atom_type=AtomType.PLANNED,
                content="周末去爬山看日出",
                importance=0.8,
                confidence=0.8,
                event_time=now + 3 * 86400,
                persona_id="大肥鱼",
            )
        )

        connection = await aiosqlite.connect(db_path)
        connection.row_factory = aiosqlite.Row
        await connection.execute(
            "CREATE TABLE documents (id INTEGER PRIMARY KEY, doc_id TEXT, text TEXT, "
            "metadata TEXT, created_at TEXT, updated_at TEXT)"
        )
        documents = [
            (
                1,
                "doc-1",
                "正文一",
                {
                    "status": "active",
                    "persona_id": "大肥鱼",
                    "create_time": now - 3600,
                    "event_start": "2026-11-12T20:00:00+08:00",
                    "topics": ["跑步", "语音"],
                    "key_facts": ["答应下次一起看烟花"],
                    "importance": 0.9,
                    "persona_summary": "主人跑步回来，累但满足。",
                    "emotion": {"labels": ["满足"], "explicit": True},
                },
            ),
            (
                2,
                "doc-2",
                "正文二",
                {
                    "status": "active",
                    "persona_id": "大肥鱼",
                    "create_time": now - 7200,
                    "event_start": "2026-11-11T20:00:00+08:00",
                    "topics": ["跑步"],
                    "key_facts": ["不要提那件私事"],
                    "importance": 0.5,
                },
            ),
            (
                3,
                "doc-3",
                "正文三",
                {
                    "status": "active",
                    "persona_id": "其他人格",
                    "create_time": now - 3600,
                    "event_start": "2026-11-12T21:00:00+08:00",
                    "topics": ["工作"],
                    "key_facts": [],
                    "importance": 0.9,
                },
            ),
        ]
        for doc_id, doc_key, text, metadata in documents:
            await connection.execute(
                "INSERT INTO documents (id, doc_id, text, metadata, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, '2026-11-12 00:00:00', '2026-11-12 00:00:00')",
                (doc_id, doc_key, text, json.dumps(metadata, ensure_ascii=False)),
            )
        await connection.commit()

        manager = RelationshipStateManager(
            SimpleNamespace(db_connection=connection),
            tmp_path,
            {"relationship_state_enabled": True, "relationship_state_window_days": 30},
        )
        await manager.initialize()
        await manager.refresh()
        state = manager.get_state("大肥鱼")
        other = manager.get_state("其他人格")
        rendered = manager.render_for_injection("大肥鱼")
        await connection.close()
        return state, other, rendered

    state, other, rendered = asyncio.run(scenario())

    assert state["themes"][0]["name"] == "跑步"
    assert state["themes"][0]["count"] == 2
    assert any("爬山" in item["text"] for item in state["open_loops"])
    assert any("烟花" in item["text"] for item in state["open_loops"])
    assert any("不要提" in item["text"] for item in state["sensitive_topics"])
    assert state["recent_events"][0]["emotion"] == ["满足"]

    assert other["themes"][0]["name"] == "工作"
    assert not other["open_loops"]

    assert "近期主题" in rendered
    assert "未完成线索" in rendered
    assert "不要假定" in rendered
    assert len(rendered) <= 520
