"""Phase B：Episode 时间线与原子巩固测试。"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import patch

import aiosqlite
import pytest

from astrbot_plugin_livingmemory.core.managers.atom_lifecycle_manager import (
    AtomLifecycleManager,
)
from astrbot_plugin_livingmemory.core.managers.memory_engine import MemoryEngine
from astrbot_plugin_livingmemory.core.models.memory_atom import AtomType, MemoryAtom
from astrbot_plugin_livingmemory.core.tools.memory_timeline_tool import (
    MemoryTimelineTool,
)
from astrbot_plugin_livingmemory.storage.atom_store import AtomStore


def _atom(content: str, *, parent: int = 1, atom_type=AtomType.FACTUAL, importance=0.8):
    return MemoryAtom(
        parent_memory_id=parent,
        atom_type=atom_type,
        content=content,
        importance=importance,
        confidence=0.8,
        persona_id="大肥鱼",
    )


def test_atom_consolidation_promotes_identity_and_merges_duplicates(tmp_path):
    store = AtomStore(str(tmp_path / "livingmemory.db"))
    identity = _atom("example-user的生日是3月2日")
    first = _atom("example-user的ID是10000000")
    duplicate = _atom("example-user的ID是10000000")
    unrelated = _atom("example-user今天吃了泡面", parent=2, importance=0.6)

    async def scenario():
        await store.initialize()
        ids = await store.insert_many([identity, first, duplicate, unrelated])
        manager = AtomLifecycleManager(
            store,
            {
                "atom_stable_enabled": True,
                "atom_duplicate_similarity": 0.6,
                "atom_stable_ttl_days": 3650,
            },
        )
        first_pass = await manager.consolidate_active_atoms()
        second_pass = await manager.consolidate_active_atoms()
        atoms = {atom_id: await store.get(atom_id) for atom_id in ids}
        return first_pass, second_pass, atoms

    first_pass, second_pass, atoms = asyncio.run(scenario())
    identity_id, first_id, duplicate_id, unrelated_id = list(atoms)

    assert first_pass["stable"] >= 2
    assert first_pass["superseded"] == 1
    assert second_pass == {"stable": 0, "superseded": 0}

    identity_atom = atoms[identity_id]
    assert identity_atom.metadata["stability"] == "stable"
    assert identity_atom.metadata["stable_reason"] == "identity_fact"
    assert identity_atom.ttl_days >= 3650

    active = atoms[first_id]
    retired = atoms[duplicate_id]
    assert active.status.value == "active"
    assert active.metadata["stability"] == "stable"
    assert retired.status.value == "superseded"
    assert retired.metadata["superseded_by_atom_id"] == first_id
    assert retired.metadata["supersede_reason"] == "near_duplicate"

    untouched = atoms[unrelated_id]
    assert untouched.status.value == "active"
    assert "stability" not in untouched.metadata


def test_list_memories_page_filters_event_window(tmp_path):
    db_path = tmp_path / "livingmemory.db"

    async def scenario():
        connection = await aiosqlite.connect(db_path)
        connection.row_factory = aiosqlite.Row
        await connection.execute(
            "CREATE TABLE documents (id INTEGER PRIMARY KEY, doc_id TEXT, text TEXT, "
            "metadata TEXT, created_at TEXT, updated_at TEXT)"
        )
        rows = [
            (1, "doc-1", "第一段", {"event_start": "2026-11-01T10:00:00+08:00",
                                    "participants": ["主人"], "topics": ["跑步"],
                                    "status": "active", "create_time": 1793500000}),
            (2, "doc-2", "第二段", {"event_start": "2026-11-02T10:00:00+08:00",
                                    "participants": ["主人"], "topics": ["语音"],
                                    "status": "active", "create_time": 1793600000}),
            (3, "doc-3", "第三段", {"event_start": "2026-11-03T10:00:00+08:00",
                                    "participants": ["朋友"], "topics": ["跑步"],
                                    "status": "active", "create_time": 1793700000}),
        ]
        for doc_id, doc_key, text, metadata in rows:
            await connection.execute(
                "INSERT INTO documents (id, doc_id, text, metadata, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, '2026-11-01 00:00:00', '2026-11-01 00:00:00')",
                (doc_id, doc_key, text, json.dumps(metadata, ensure_ascii=False)),
            )
        await connection.commit()

        engine = object.__new__(MemoryEngine)
        engine.db_connection = connection
        result = await engine.list_memories_page(
            date_start="2026-11-02",
            date_end="2026-11-03",
            participant="主人",
            topic="语音",
            sort_key="event_asc",
            page=1,
            page_size=10,
        )
        await connection.close()
        return result

    result = asyncio.run(scenario())
    assert result["total"] == 1
    assert result["items"][0]["id"] == 2


def test_timeline_tool_formats_episodes():
    class FakeEngine:
        async def list_memories_page(self, **kwargs):
            assert kwargs["date_start"] == "2026-11-01"
            assert kwargs["date_end"] == "2026-11-03"
            assert kwargs["sort_key"] == "event_asc"
            return {
                "items": [
                    {
                        "id": 7,
                        "text": "正文",
                        "metadata": json.dumps(
                            {
                                "persona_summary": "主人去骑了21公里，回来累但满足。",
                                "event_start": "2026-11-02T18:30:00+08:00",
                                "event_end": "2026-11-02T19:10:00+08:00",
                                "participant_identities": [
                                    {"display_name": "主人", "is_bot": False},
                                    {"display_name": "大肥鱼", "is_bot": True},
                                ],
                                "topics": ["跑步"],
                                "key_facts": ["骑了21公里"],
                                "emotion": {
                                    "labels": ["累", "满足"],
                                    "explicit": True,
                                    "inferred": False,
                                    "evidence": "累死了但挺爽",
                                },
                                "importance": 0.8,
                                "confidence": 0.85,
                            },
                            ensure_ascii=False,
                        ),
                    }
                ]
            }

    tool = MemoryTimelineTool(
        context=object(),
        config_manager=object(),
        memory_engine=FakeEngine(),
    )
    wrapper = SimpleNamespace(context=SimpleNamespace(event=object()))

    with patch(
        "astrbot_plugin_livingmemory.core.tools.memory_timeline_tool.resolve_tool_access",
        new=lambda *args, **kwargs: _fake_access(),
    ), patch(
        "astrbot_plugin_livingmemory.core.tools.memory_timeline_tool.adapter_access",
        new=lambda *args, **kwargs: _null_context(),
    ):
        raw = asyncio.run(
            tool.call(
                wrapper,
                date_start="2026-11-01",
                date_end="2026-11-03",
                person="主人",
                topic="跑步",
            )
        )

    payload = json.loads(raw)
    assert payload["ok"] is True
    assert payload["count"] == 1
    episode = payload["episodes"][0]
    assert episode["date_start"].startswith("2026-11-02T18:30:00")
    assert episode["participants"] == ["主人", "我"]
    assert episode["emotion"]["labels"] == ["累", "满足"]
    assert episode["confidence"] == 0.85


async def _fake_access():
    return object(), "scope", "大肥鱼", None


class _null_context:
    def __enter__(self):
        return None

    def __exit__(self, exc_type, exc, tb):
        return False
