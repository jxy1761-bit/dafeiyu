"""Phase A 记忆证据与情绪规范化工具。

纯函数，不依赖 AstrBot，便于单测和迁移脚本复用。
"""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime
from typing import Any

from .emotion_taxonomy import canonical_emotion_labels


_MAX_QUOTE_CHARS = 200
_MIN_QUOTE_CHARS = 2
_MAX_QUOTES = 3
_MAX_EMOTION_LABELS = 3


def normalize_for_match(text: Any) -> str:
    """把文本规范成可做“逐字引用”比对的形式。

    只抹平空白、全半角与引号差异，保留标点，避免跨句误匹配。
    """
    value = unicodedata.normalize("NFKC", str(text or ""))
    value = (
        value.replace("“", '"')
        .replace("”", '"')
        .replace("‘", "'")
        .replace("’", "'")
    )
    return re.sub(r"\s+", "", value).casefold()


def clean_quote(text: Any) -> str:
    """清理模型返回的引用片段，保留原文措辞。"""
    value = str(text or "").strip()
    if not value:
        return ""
    value = value.strip(" \t\r\n\"'“”‘’「」『』")
    value = re.sub(r"^(?:\.\.\.|…)+|(?:\.\.\.|…)+$", "", value).strip()
    # 中间带省略号的通常是概括而非原话，直接丢弃。
    if "…" in value or "..." in value:
        return ""
    return value[: _MAX_QUOTE_CHARS + 1]


def sanitize_evidence_quotes(
    quotes: Any, conversation_text: str
) -> tuple[list[str], int]:
    """只保留能在原始对话中逐字找到的引用。

    Returns:
        (kept, dropped_count)
    """
    if not isinstance(quotes, (list, tuple)):
        return [], 0

    haystack = normalize_for_match(conversation_text)
    kept: list[str] = []
    dropped = 0
    for raw in quotes:
        quote = clean_quote(raw)
        if not quote:
            dropped += 1
            continue
        needle = normalize_for_match(quote)
        if len(quote) < _MIN_QUOTE_CHARS or len(quote) > _MAX_QUOTE_CHARS:
            dropped += 1
            continue
        if needle and needle in haystack:
            if quote not in kept:
                kept.append(quote)
            if len(kept) >= _MAX_QUOTES:
                break
        else:
            dropped += 1
    return kept, dropped


def _clamp_float(value: Any, low: float, high: float, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def _emotion_labels(value: Any) -> list[str]:
    if isinstance(value, str):
        items = re.split(r"[,，、/;；]+", value)
    elif isinstance(value, (list, tuple)):
        items = list(value)
    else:
        items = []
    labels: list[str] = []
    for item in items:
        label = str(item or "").strip()[:12]
        if label and label not in labels:
            labels.append(label)
        if len(labels) >= _MAX_EMOTION_LABELS:
            break
    return labels


def normalize_emotion(raw: Any) -> dict[str, Any] | None:
    """把 LLM 输出的情绪对象规范化；无有效内容时返回 None。"""
    if not isinstance(raw, dict):
        return None

    labels = _emotion_labels(raw.get("labels") or raw.get("label"))
    trigger = str(raw.get("trigger") or "").strip()[:80]
    evidence = clean_quote(raw.get("evidence"))
    valence = _clamp_float(raw.get("valence"), -1.0, 1.0, 0.0)
    arousal = _clamp_float(raw.get("arousal"), 0.0, 1.0, 0.0)
    intensity = _clamp_float(raw.get("intensity"), 0.0, 1.0, 0.0)
    confidence = _clamp_float(raw.get("confidence"), 0.0, 1.0, 0.5)

    if not labels and not trigger and not evidence:
        return None

    explicit = bool(raw.get("explicit"))
    # 没有可核验的原话时一律按推断处理，避免把推测写成事实。
    if explicit and not evidence:
        explicit = False
    if not explicit:
        confidence = min(confidence, 0.6)

    return {
        "labels": labels,
        "canonical_labels": canonical_emotion_labels(labels),
        "valence": round(valence, 3),
        "arousal": round(arousal, 3),
        "intensity": round(intensity, 3),
        "trigger": trigger,
        "evidence": evidence,
        "explicit": explicit,
        "inferred": not explicit,
        "confidence": round(confidence, 3),
    }


def verify_emotion_evidence(
    emotion: dict[str, Any] | None, conversation_text: str
) -> dict[str, Any] | None:
    """核验情绪证据是否为原话；核验失败则降级为推断。"""
    if not isinstance(emotion, dict):
        return None
    result = dict(emotion)
    evidence = clean_quote(result.get("evidence"))
    if evidence:
        kept, _ = sanitize_evidence_quotes([evidence], conversation_text)
        if kept:
            result["evidence"] = kept[0]
        else:
            result["evidence"] = ""
    if not result.get("evidence"):
        result["explicit"] = False
    result["inferred"] = not bool(result.get("explicit"))
    if result["inferred"]:
        result["confidence"] = min(
            _clamp_float(result.get("confidence"), 0.0, 1.0, 0.5), 0.6
        )
    return result


def normalize_emotion_moments(raw: Any, max_items: int = 5) -> list[dict[str, Any]]:
    """把 LLM 输出的逐条情绪瞬间规范化；只保留有标签或证据的条目。"""
    if not isinstance(raw, list):
        return []
    moments: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        emotion = normalize_emotion(item)
        if not emotion:
            continue
        moment = dict(emotion)
        moment["time"] = str(item.get("time") or "").strip()[:16]
        moments.append(moment)
        if len(moments) >= max_items:
            break
    return moments


def verify_emotion_moments(
    moments: Any, conversation_text: str
) -> list[dict[str, Any]]:
    """核验每条情绪瞬间的原话证据；证据不成立时降级为推断。"""
    if not isinstance(moments, list):
        return []
    verified: list[dict[str, Any]] = []
    for item in moments:
        moment = verify_emotion_evidence(item, conversation_text)
        if not moment:
            continue
        if not moment.get("labels") and not moment.get("evidence"):
            continue
        verified.append(moment)
        if len(verified) >= 5:
            break
    return verified


def compute_confidence(
    quality: str,
    kept_quotes: int,
    dropped_quotes: int,
    *,
    base: float | None = None,
) -> tuple[float, str, str]:
    """计算记忆置信度。

    这是工程启发式：质量等级给底分，逐字证据加分，被丢弃的“伪引用”减分。
    """
    if base is None:
        base = 0.78 if quality == "normal" else 0.5
    score = float(base)
    if kept_quotes > 0:
        score += 0.07
        status = "verified"
        basis = f"quality:{quality}+verbatim_evidence"
    elif dropped_quotes > 0:
        score -= 0.08
        status = "dropped"
        basis = f"quality:{quality}+evidence_dropped"
    else:
        status = "none"
        basis = f"quality:{quality}"
    return round(max(0.0, min(0.95, score)), 2), status, basis


def _local_iso(value: datetime) -> str:
    return value.astimezone().isoformat()


def _epoch_to_datetime(value: Any) -> datetime | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number > 1e12:  # 毫秒时间戳
        number /= 1000.0
    if number <= 0:
        return None
    try:
        return datetime.fromtimestamp(number)
    except (OverflowError, OSError, ValueError):
        return None


def derive_event_window(
    metadata: dict[str, Any],
    created_at: str | None = None,
) -> tuple[str | None, str | None, str]:
    """为历史记忆推导事件时间范围。

    优先级：已有 source_time_* > time_tags 日期 > create_time > created_at。
    """
    if not isinstance(metadata, dict):
        metadata = {}

    start = metadata.get("source_time_start") or metadata.get("event_start")
    end = metadata.get("source_time_end") or metadata.get("event_end")
    if start and end:
        return str(start), str(end), "messages"

    tags = metadata.get("time_tags")
    if isinstance(tags, (list, tuple)):
        dates = []
        for tag in tags:
            try:
                dates.append(datetime.strptime(str(tag)[:10], "%Y-%m-%d"))
            except ValueError:
                continue
        if dates:
            dates.sort()
            begin = dates[0].replace(hour=0, minute=0, second=0, microsecond=0)
            finish = dates[-1].replace(hour=23, minute=59, second=59, microsecond=0)
            return _local_iso(begin), _local_iso(finish), "time_tags"

    moment = _epoch_to_datetime(metadata.get("create_time"))
    if moment is None and created_at:
        try:
            moment = datetime.fromisoformat(str(created_at))
        except ValueError:
            moment = None
    if moment is not None:
        iso = _local_iso(moment)
        return iso, iso, "create_time"

    return None, None, "unknown"


def derive_participants(metadata: dict[str, Any]) -> list[str] | None:
    """从 participant_identities 推导参与者名单（仅当缺失时）。"""
    if not isinstance(metadata, dict):
        return None
    existing = metadata.get("participants")
    if isinstance(existing, list) and existing:
        return None

    identities = metadata.get("participant_identities")
    if not isinstance(identities, list) or not identities:
        return None

    names: list[str] = []
    for item in identities:
        if not isinstance(item, dict):
            continue
        name = str(item.get("display_name") or item.get("sender_id") or "").strip()
        if not name:
            continue
        if item.get("is_bot"):
            name = "我"
        if name not in names:
            names.append(name)
    return names or None
