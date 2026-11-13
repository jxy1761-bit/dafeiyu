"""情绪标签归一化表：显示保留原标签，召回使用 canonical_labels。"""

from __future__ import annotations

from typing import Iterable

CANONICAL_SYNONYMS: dict[str, tuple[str, ...]] = {
    "开心": (
        "开心",
        "高兴",
        "快乐",
        "愉快",
        "轻松",
        "调皮",
        "撒娇",
        "宠溺",
        "亲昵",
        "好胜",
    ),
    "满足": ("满足", "满意", "自豪", "得意", "欣慰", "温暖", "感动", "放心"),
    "焦虑": ("焦虑", "担心", "关切", "压力", "内耗", "紧张", "不安"),
    "难过": (
        "难过",
        "伤心",
        "失望",
        "难受",
        "遗憾",
        "不舍",
        "沮丧",
        "低落",
        "失落",
        "心疼",
        "惭愧",
    ),
    "生气": ("生气", "愤怒", "讨厌", "不服", "恼火", "气死"),
    "害怕": ("害怕", "恐惧", "惊吓"),
    "委屈": ("委屈", "冤枉", "憋屈", "羞耻"),
    "孤独": ("孤独", "孤单", "寂寞"),
    "烦": ("烦", "烦躁", "郁闷"),
    "累": ("累", "疲惫", "乏力"),
}

_LABEL_TO_CANONICAL: dict[str, str] = {}
for canonical, synonyms in CANONICAL_SYNONYMS.items():
    for synonym in synonyms:
        _LABEL_TO_CANONICAL.setdefault(synonym, canonical)


def canonical_emotion_labels(labels: Iterable[str]) -> list[str]:
    result: list[str] = []
    for label in labels or []:
        text = str(label or "").strip()
        if not text:
            continue
        canonical = _LABEL_TO_CANONICAL.get(text, text)
        if canonical not in result:
            result.append(canonical)
    return result


__all__ = ["CANONICAL_SYNONYMS", "canonical_emotion_labels"]
