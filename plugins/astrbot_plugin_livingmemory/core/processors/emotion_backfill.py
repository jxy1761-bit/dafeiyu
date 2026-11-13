"""历史情绪回填：规则抽取与回填结果合并。

只处理保留原文的 memory_sources；没有原文的旧记忆不伪造逐条瞬间。
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Iterable

from ..retrieval.cue_extraction import EMOTION_LEXICON, emotion_word_present

_VALENCE = {
    "开心": 0.6,
    "兴奋": 0.7,
    "满足": 0.5,
    "难过": -0.6,
    "焦虑": -0.5,
    "生气": -0.6,
    "委屈": -0.5,
    "害怕": -0.5,
    "孤独": -0.5,
    "烦": -0.4,
    "累": -0.3,
    "心疼": -0.4,
}


def _sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[。！？!?；;])\s*", str(text or ""))
    return [part.strip() for part in parts if part.strip()]


def extract_rule_moments(
    messages: Iterable[dict[str, Any]], max_items: int = 5
) -> list[dict[str, Any]]:
    """从原文消息里提取显式情绪瞬间；不调用模型。"""
    moments: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for message in messages:
        if not isinstance(message, dict):
            continue
        if message.get("role") != "user":
            continue
        if (message.get("metadata") or {}).get("is_bot_message"):
            continue
        content = str(message.get("content") or "")
        try:
            timestamp = float(message.get("timestamp") or 0)
        except (TypeError, ValueError):
            timestamp = 0.0
        if not content or timestamp <= 0:
            continue
        time_text = datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M")
        for sentence in _sentences(content):
            for label, synonyms in EMOTION_LEXICON.items():
                matched = next(
                    (
                        word
                        for word in synonyms
                        if emotion_word_present(
                            sentence, label, word, require_first_person=True
                        )
                    ),
                    None,
                )
                if not matched:
                    continue
                key = (time_text, label, sentence[:80])
                if key in seen:
                    continue
                seen.add(key)
                moments.append(
                    {
                        "time": time_text,
                        "labels": [label],
                        "valence": _VALENCE.get(label, 0.0),
                        "arousal": 0.4,
                        "intensity": 0.5,
                        "trigger": "",
                        "evidence": sentence[:120],
                        "explicit": True,
                        "inferred": False,
                        "confidence": 0.75,
                        "source": "rule_backfill",
                    }
                )
                if len(moments) >= max_items:
                    return moments
    return moments


def merge_moments(
    existing: Any, additions: list[dict[str, Any]], max_items: int = 5
) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for item in list(existing or []) + list(additions or []):
        if not isinstance(item, dict):
            continue
        key = (str(item.get("time") or ""), tuple(str(v) for v in item.get("labels") or []))
        evidence = str(item.get("evidence") or "")[:40]
        dedupe = (key[0], evidence or str(key[1]))
        if dedupe in seen:
            continue
        seen.add(dedupe)
        merged.append(item)
        if len(merged) >= max_items:
            break
    return merged


__all__ = ["extract_rule_moments", "merge_moments"]
