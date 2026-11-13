"""Phase H2：历史情绪规则回填测试。"""

from __future__ import annotations

from astrbot_plugin_livingmemory.core.processors.emotion_backfill import (
    extract_rule_moments,
    merge_moments,
)


def _message(text: str, ts: float = 1794400000.0) -> dict:
    return {
        "role": "user",
        "content": text,
        "timestamp": ts,
        "metadata": {"is_bot_message": False},
    }


def test_rule_backfill_keeps_first_person_emotions_only():
    messages = [
        _message("我真的真的已经受够孤独了。"),
        _message("我有点害怕，怕失去。"),
        _message("哪怕家里也跟我说没必要着急。"),
        _message("今天累积了很多经验。"),
        _message("你想要的要求我都尽全力满足。"),
        _message("我很满足，心里踏实。"),
    ]
    moments = extract_rule_moments(messages)
    labels = [item["labels"][0] for item in moments]
    assert "孤独" in labels
    assert "害怕" in labels
    assert labels.count("满足") == 1
    satisfied = next(item for item in moments if item["labels"][0] == "满足")
    assert "很满足" in satisfied["evidence"]
    assert all(item["explicit"] for item in moments)
    assert all(item["evidence"] for item in moments)


def test_merge_moments_dedupes_same_time_and_evidence():
    first = [
        {"time": "2026-11-11 20:10", "labels": ["累"], "evidence": "我好累"},
        {"time": "2026-11-11 20:11", "labels": ["开心"], "evidence": "我挺开心"},
    ]
    merged = merge_moments(first, [first[0], first[1]])
    assert len(merged) == 2
