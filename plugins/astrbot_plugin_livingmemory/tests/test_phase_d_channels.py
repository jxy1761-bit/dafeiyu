"""Phase D：实体与情绪召回通道测试。"""

from __future__ import annotations

import asyncio
import json

import aiosqlite

from astrbot_plugin_livingmemory.core.managers.memory_engine import MemoryEngine
from astrbot_plugin_livingmemory.core.retrieval.cue_extraction import (
    emotion_sentiments,
    extract_emotion_cues,
    extract_entity_cues,
)


def test_cue_extraction_is_conservative():
    assert extract_emotion_cues("今天好累") == ["累"]
    assert extract_emotion_cues("上次我很难过") == ["难过"]
    assert extract_emotion_cues("今天天气不错") == []
    assert emotion_sentiments(["难过"]) == ["negative"]
    assert emotion_sentiments(["开心"]) == ["positive"]

    entities = extract_entity_cues(
        "还记得西湖那次吗", ["西湖", "烟花", "主人", "我"]
    )
    assert entities == ["西湖"]


async def _make_docs_connection(rows):
    connection = await aiosqlite.connect(":memory:")
    connection.row_factory = aiosqlite.Row
    await connection.execute(
        "CREATE TABLE documents (id INTEGER PRIMARY KEY, doc_id TEXT, text TEXT, "
        "metadata TEXT, created_at TEXT, updated_at TEXT)"
    )
    for doc_id, text, metadata in rows:
        await connection.execute(
            "INSERT INTO documents (id, doc_id, text, metadata, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, '2026-11-01 00:00:00', '2026-11-01 00:00:00')",
            (doc_id, f"doc-{doc_id}", text, json.dumps(metadata, ensure_ascii=False)),
        )
    await connection.commit()
    return connection


def test_emotion_channel_uses_labels_and_sentiment_fallback():
    async def scenario():
        connection = await _make_docs_connection(
            [
                (
                    1,
                    "那天主人说心里很难过，我陪他坐了一会儿。",
                    {
                        "status": "active",
                        "persona_id": "大肥鱼",
                        "importance": 0.8,
                        "sentiment": "negative",
                        "emotion": {"labels": ["难过"], "explicit": True},
                        "create_time": 1794000000,
                    },
                ),
                (
                    2,
                    "主人今天吃了好吃的，很开心。",
                    {
                        "status": "active",
                        "persona_id": "大肥鱼",
                        "importance": 0.6,
                        "sentiment": "positive",
                        "create_time": 1794000100,
                    },
                ),
                (
                    3,
                    "普通的一天，什么都没发生。",
                    {
                        "status": "active",
                        "persona_id": "大肥鱼",
                        "importance": 0.5,
                        "sentiment": "neutral",
                        "create_time": 1794000200,
                    },
                ),
            ]
        )
        engine = object.__new__(MemoryEngine)
        engine.db_connection = connection
        results = await engine._merge_emotion_candidates(
            [], "上次我难过的时候你记得吗", 5, None, "大肥鱼"
        )
        await connection.close()
        return results

    results = asyncio.run(scenario())
    assert [item.doc_id for item in results] == [1]
    assert results[0].score_breakdown["emotion"] == 1.0


def test_emotion_channel_matches_emotion_moments():
    async def scenario():
        connection = await _make_docs_connection(
            [
                (
                    1,
                    "那天主人说心里很难过，我陪他坐了一会儿。",
                    {
                        "status": "active",
                        "persona_id": "大肥鱼",
                        "importance": 0.8,
                        "sentiment": "neutral",
                        "emotion": {},
                        "emotion_moments": [
                            {
                                "time": "2026-11-11 20:10",
                                "labels": ["难过"],
                                "evidence": "心里很难过",
                                "explicit": True,
                            }
                        ],
                        "create_time": 1794000000,
                    },
                )
            ]
        )
        engine = object.__new__(MemoryEngine)
        engine.db_connection = connection
        results = await engine._merge_emotion_candidates(
            [], "上次我难过的时候你记得吗", 5, None, "大肥鱼"
        )
        await connection.close()
        return results

    results = asyncio.run(scenario())
    assert [item.doc_id for item in results] == [1]


def test_entity_channel_matches_known_topic():
    async def scenario():
        connection = await _make_docs_connection(
            [
                (
                    1,
                    "去年去西湖拍烟花，晚上特别冷。",
                    {
                        "status": "active",
                        "persona_id": "大肥鱼",
                        "importance": 0.85,
                        "create_time": 1794000000,
                    },
                ),
                (
                    2,
                    "今天在家补觉。",
                    {
                        "status": "active",
                        "persona_id": "大肥鱼",
                        "importance": 0.6,
                        "create_time": 1794000100,
                    },
                ),
            ]
        )
        await connection.execute(
            "CREATE TABLE graph_nodes (id INTEGER PRIMARY KEY, node_key TEXT, "
            "node_type TEXT, canonical_value TEXT)"
        )
        await connection.executemany(
            "INSERT INTO graph_nodes (node_key, node_type, canonical_value) VALUES (?, ?, ?)",
            [
                ("topic:西湖", "topic", "西湖"),
                ("topic:烟花", "topic", "烟花"),
                ("person:主人", "person", "主人"),
            ],
        )
        await connection.commit()

        engine = object.__new__(MemoryEngine)
        engine.db_connection = connection
        results = await engine._merge_entity_candidates(
            [], "还记得西湖那次吗", 5, None, "大肥鱼"
        )
        await connection.close()
        return results

    results = asyncio.run(scenario())
    assert [item.doc_id for item in results] == [1]
    assert results[0].score_breakdown["entity"] == 1.0
    assert "西湖" in results[0].score_breakdown["entities"]
