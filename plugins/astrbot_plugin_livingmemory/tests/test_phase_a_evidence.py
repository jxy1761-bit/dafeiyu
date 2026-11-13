"""Phase A：证据、情绪、事件时间与注入口径测试。"""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiosqlite
import pytest

from astrbot_plugin_livingmemory.core.models.conversation_models import Message
from astrbot_plugin_livingmemory.core.processors.memory_processor import MemoryProcessor
from astrbot_plugin_livingmemory.core.utils.formatting import (
    format_memories_for_injection,
)
from astrbot_plugin_livingmemory.evidence_utils import (
    compute_confidence,
    derive_event_window,
    normalize_emotion,
    sanitize_evidence_quotes,
)
from astrbot_plugin_livingmemory.storage.db_migration import DBMigration


class _DummyLLMProvider:
    def __init__(self, completion_text: str):
        self._completion_text = completion_text
        self.text_chat = AsyncMock(side_effect=self._chat)

    async def _chat(self, prompt: str, system_prompt: str):
        return SimpleNamespace(completion_text=self._completion_text)


def _messages() -> list[Message]:
    now = time.time()
    return [
        Message(
            id=1,
            session_id="s1",
            role="user",
            content="今天骑了21公里，累死了但挺爽",
            sender_id="u1",
            sender_name="主人",
            platform="test",
            timestamp=now - 600,
        ),
        Message(
            id=2,
            session_id="s1",
            role="assistant",
            content="那你快歇会儿，别硬撑。",
            sender_id="bot",
            sender_name="大肥鱼",
            platform="test",
            timestamp=now - 540,
            metadata={"is_bot_message": True},
        ),
        Message(
            id=3,
            session_id="s1",
            role="user",
            content="心里挺满足的，晚上想吃点好的",
            sender_id="u1",
            sender_name="主人",
            platform="test",
            timestamp=now - 480,
        ),
    ]


def _emotion_payload() -> dict:
    return {
        "labels": ["累", "满足"],
        "valence": 0.4,
        "arousal": 0.3,
        "intensity": 0.6,
        "trigger": "跑步5公里",
        "evidence": "今天骑了21公里，累死了但挺爽",
        "explicit": True,
        "confidence": 0.8,
    }


def test_verbatim_evidence_is_kept_and_confidence_raised():
    llm = _DummyLLMProvider(
        json.dumps(
            {
                "summary": "主人今天骑了21公里，说累但满足，我让他先歇着。",
                "topics": ["跑步", "疲惫"],
                "key_facts": ["主人今天骑了21公里", "主人说累但挺爽"],
                "sentiment": "positive",
                "importance": 0.7,
                "emotion": _emotion_payload(),
                "evidence_quotes": [
                    "今天骑了21公里，累死了但挺爽",
                    "心里挺满足的",
                ],
            },
            ensure_ascii=False,
        )
    )
    processor = MemoryProcessor(llm_provider=llm, context=None)

    _, metadata, _ = asyncio.run(
        processor.process_conversation(messages=_messages(), is_group_chat=False)
    )

    assert metadata["phase_a_version"] == 1
    assert metadata["evidence_status"] == "verified"
    assert len(metadata["evidence_quotes"]) == 2
    assert metadata["confidence"] == 0.85
    assert metadata["emotion"]["explicit"] is True
    assert metadata["emotion"]["inferred"] is False
    assert metadata["event_start"]
    assert metadata["event_end"]
    assert metadata["event_time_source"] == "messages"


def test_emotion_moments_keep_verified_and_drop_unverifiable():
    llm = _DummyLLMProvider(
        json.dumps(
            {
                "summary": "主人今天骑了21公里，说累但满足，我让他先歇着。",
                "topics": ["跑步"],
                "key_facts": ["主人今天骑了21公里"],
                "sentiment": "positive",
                "importance": 0.7,
                "emotion": None,
                "emotion_moments": [
                    {
                        "time": "2025-11-19 14:30",
                        "labels": ["累", "满足"],
                        "valence": 0.4,
                        "arousal": 0.4,
                        "intensity": 0.6,
                        "trigger": "跑步5公里",
                        "evidence": "今天骑了21公里，累死了但挺爽",
                        "explicit": True,
                        "confidence": 0.8,
                    },
                    {
                        "time": "2025-11-19 14:31",
                        "labels": [],
                        "evidence": "编的原话",
                        "explicit": True,
                        "confidence": 0.9,
                    },
                ],
                "evidence_quotes": [],
            },
            ensure_ascii=False,
        )
    )
    processor = MemoryProcessor(llm_provider=llm, context=None)

    _, metadata, _ = asyncio.run(
        processor.process_conversation(messages=_messages(), is_group_chat=False)
    )

    assert len(metadata["emotion_moments"]) == 1
    moment = metadata["emotion_moments"][0]
    assert moment["labels"] == ["累", "满足"]
    assert moment["time"] == "2025-11-19 14:30"
    assert moment["explicit"] is True


def test_claims_are_validated_against_conversation():
    llm = _DummyLLMProvider(
        json.dumps(
            {
                "summary": "主人今天骑了21公里，说累但满足。",
                "topics": ["跑步"],
                "key_facts": ["主人今天骑了21公里"],
                "sentiment": "positive",
                "importance": 0.7,
                "claims": [
                    {
                        "subject": "主人今日跑步",
                        "predicate": "是",
                        "value": "21公里",
                        "polarity": "positive",
                        "quote": "今天骑了21公里，累死了但挺爽",
                        "confidence": 0.9,
                    },
                    {
                        "subject": "主人今日跑步",
                        "predicate": "是",
                        "value": "99公里",
                        "polarity": "positive",
                        "quote": "编造的原话",
                        "confidence": 0.9,
                    },
                ],
                "evidence_quotes": ["今天骑了21公里，累死了但挺爽"],
            },
            ensure_ascii=False,
        )
    )
    processor = MemoryProcessor(llm_provider=llm, context=None)

    _, metadata, _ = asyncio.run(
        processor.process_conversation(messages=_messages(), is_group_chat=False)
    )

    claims = metadata["claims"]
    assert len(claims) == 2
    valid = next(item for item in claims if item["value"] == "21公里")
    forged = next(item for item in claims if item["value"] == "99公里")
    assert valid["quote"] == "今天骑了21公里，累死了但挺爽"
    assert valid["confidence"] == 0.9
    assert forged["quote"] == ""
    assert forged["confidence"] <= 0.6


def test_paraphrased_evidence_is_dropped_and_emotion_downgraded():
    llm = _DummyLLMProvider(
        json.dumps(
            {
                "summary": "主人今天骑了二十多公里，说很累也很满足。",
                "topics": ["跑步"],
                "key_facts": ["主人跑步超过五公里"],
                "sentiment": "positive",
                "importance": 0.6,
                "emotion": {
                    "labels": ["累"],
                    "valence": 0.2,
                    "evidence": "骑了二十多公里，累坏了",
                    "explicit": True,
                    "confidence": 0.9,
                },
                "evidence_quotes": ["骑了二十多公里"],
            },
            ensure_ascii=False,
        )
    )
    processor = MemoryProcessor(llm_provider=llm, context=None)

    _, metadata, _ = asyncio.run(
        processor.process_conversation(messages=_messages(), is_group_chat=False)
    )

    assert metadata["evidence_quotes"] == []
    assert metadata["evidence_status"] == "dropped"
    assert metadata["confidence"] == 0.7
    assert metadata["emotion"]["explicit"] is False
    assert metadata["emotion"]["inferred"] is True
    assert metadata["emotion"]["evidence"] == ""
    assert metadata["emotion"]["confidence"] <= 0.6


def test_invalid_emotion_is_dropped():
    assert normalize_emotion(None) is None
    assert normalize_emotion("开心") is None
    assert normalize_emotion({"labels": [], "trigger": ""}) is None


def test_evidence_sanitizer_rejects_ellipsis_and_accepts_short_quote():
    kept, dropped = sanitize_evidence_quotes(
        ["我好累", "他说了很长一段……", "编的话"], "主人说：我好累"
    )
    assert kept == ["我好累"]
    assert dropped == 2


def test_confidence_bounds():
    assert compute_confidence("normal", 0, 0)[0] == 0.78
    assert compute_confidence("normal", 1, 0)[0] == 0.85
    assert compute_confidence("low", 0, 0)[0] == 0.5
    assert compute_confidence("normal", 0, 1)[0] == 0.7


def test_event_window_prefers_messages_then_tags():
    start, end, source = derive_event_window(
        {"source_time_start": "2026-11-13T10:00:00+08:00",
         "source_time_end": "2026-11-13T10:30:00+08:00"}
    )
    assert source == "messages"
    assert start.endswith("10:00:00+08:00")
    assert end.endswith("10:30:00+08:00")

    start, end, source = derive_event_window(
        {"time_tags": ["2026-11-01", "2026-11-03"]}
    )
    assert source == "time_tags"
    assert start.startswith("2026-11-01T00:00:00")
    assert end.startswith("2026-11-03T23:59:59")


def test_injection_shows_id_confidence_time_and_inferred_marker():
    memories = [
        {
            "id": 42,
            "content": "主人说今天跑步很累但满足。",
            "score": 0.9,
            "timestamp": 1794400000,
            "metadata": {
                "importance": 0.8,
                "confidence": 0.85,
                "event_start": "2026-11-13T18:30:00+08:00",
                "event_end": "2026-11-13T19:10:00+08:00",
                "topics": ["跑步"],
                "evidence_quotes": ["今天骑了21公里，累死了但挺爽"],
                "emotion": {
                    "labels": ["累", "满足"],
                    "valence": 0.4,
                    "arousal": 0.3,
                    "intensity": 0.6,
                    "trigger": "跑步5公里",
                    "evidence": "",
                    "explicit": False,
                    "inferred": True,
                    "confidence": 0.5,
                },
                "emotion_moments": [
                    {
                        "time": "2026-11-13 19:41",
                        "labels": ["累"],
                        "evidence": "今天骑了21公里，累死了但挺爽",
                        "explicit": True,
                        "inferred": False,
                        "confidence": 0.8,
                    }
                ],
            },
        }
    ]

    text = format_memories_for_injection(memories)

    assert "id: 42" in text
    assert "Confidence: 0.85" in text
    assert "Event time: 2026-11-13 18:30-19:10" in text
    assert "Emotion (INFERRED, not the user's words" in text
    assert "Emotion moment 19:41" in text
    assert 'Evidence (verbatim): "今天骑了21公里，累死了但挺爽"' in text


def test_v9_migration_backfills_without_overwriting(tmp_path):
    db_path = tmp_path / "livingmemory.db"
    identity_metadata = {
        "summary_quality": "normal",
        "participant_identities": [
            {"sender_id": "u1", "display_name": "主人", "is_bot": False},
            {"sender_id": "bot", "display_name": "大肥鱼", "is_bot": True},
        ],
    }
    tagged_metadata = {"summary_quality": "low", "time_tags": ["2026-11-01"]}

    async def scenario():
        async with aiosqlite.connect(db_path) as db:
            await db.execute(
                "CREATE TABLE documents (id INTEGER PRIMARY KEY, doc_id TEXT, text TEXT, "
                "metadata TEXT, created_at TEXT, updated_at TEXT)"
            )
            await db.execute(
                "INSERT INTO documents (id, doc_id, text, metadata, created_at, updated_at) "
                "VALUES (1, 'doc-1', '正文', ?, '2026-11-13 09:00:00.000000', "
                "'2026-11-13 09:00:00.000000')",
                (json.dumps(identity_metadata, ensure_ascii=False),),
            )
            await db.execute(
                "INSERT INTO documents (id, doc_id, text, metadata, created_at, updated_at) "
                "VALUES (2, 'doc-2', '正文', ?, '2026-11-13 09:00:00.000000', "
                "'2026-11-13 09:00:00.000000')",
                (json.dumps(tagged_metadata, ensure_ascii=False),),
            )
            await db.commit()

        migration = DBMigration(str(db_path))
        await migration._migrate_v8_to_v9(None)
        await migration._migrate_v8_to_v9(None)  # 幂等

        async with aiosqlite.connect(db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT id, metadata FROM documents")
            fetched = await cursor.fetchall()
            return {row["id"]: json.loads(row["metadata"]) for row in fetched}

    rows = asyncio.run(scenario())

    first, second = rows[1], rows[2]
    assert first["phase_a_version"] == 1
    assert first["confidence"] == 0.78
    assert first["event_start"]
    assert first["participants"] == ["主人", "我"]

    assert second["event_start"].startswith("2026-11-01T00:00:00")
    assert second["event_end"].startswith("2026-11-01T23:59:59")
    assert second["event_time_source"] == "time_tags"
    assert second["confidence"] == 0.5
