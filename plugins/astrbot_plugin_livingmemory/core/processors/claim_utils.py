"""结构化 claim：subject / predicate / value / polarity。

总结时顺带产出，写入原子元数据；同主语+谓词、不同值或极性即可判定冲突，
不需要再猜文本相似度。
"""

from __future__ import annotations

import re
from typing import Any

from ..models.memory_atom import AtomType, MemoryAtom
from ...evidence_utils import clean_quote, sanitize_evidence_quotes

_PREFERENCE_PREDICATES = ("喜欢", "不喜欢", "讨厌", "偏爱", "最爱", "爱吃", "怕")
_PLANNED_PREDICATES = ("计划", "打算", "准备", "约定", "答应", "要", "想")
_NEGATIVE_WORDS = ("不", "没", "无", "未", "别", "讨厌", "拒绝")


def _clean(value: Any, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    text = text.strip("，。；;,.!?！？:：\"'“”‘’「」『』")
    return text[:limit]


def normalize_claim(
    raw: Any, conversation_text: str | None = None
) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    subject = _clean(raw.get("subject"), 20)
    predicate = _clean(raw.get("predicate"), 10)
    value = _clean(raw.get("value"), 40)
    quote = clean_quote(raw.get("quote"))
    if not subject or not predicate:
        return None

    polarity = str(raw.get("polarity") or "").strip().lower()
    if polarity not in {"positive", "negative"}:
        polarity = (
            "negative"
            if any(word in predicate or word in value for word in _NEGATIVE_WORDS)
            else "positive"
        )

    try:
        confidence = float(raw.get("confidence", 0.7))
    except (TypeError, ValueError):
        confidence = 0.7
    confidence = max(0.0, min(1.0, confidence))

    if quote and conversation_text:
        kept, _ = sanitize_evidence_quotes([quote], conversation_text)
        if kept:
            quote = kept[0]
        else:
            quote = ""
            confidence = min(confidence, 0.6)
    elif not quote:
        confidence = min(confidence, 0.6)

    return {
        "subject": subject,
        "predicate": predicate,
        "value": value,
        "polarity": polarity,
        "quote": quote,
        "confidence": round(confidence, 3),
    }


def normalize_claims(
    raw: Any, conversation_text: str | None = None, max_items: int = 8
) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    claims: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for item in raw:
        claim = normalize_claim(item, conversation_text)
        if not claim:
            continue
        key = claim_key(claim) + (claim["value"].casefold(), claim["polarity"])
        if key in seen:
            continue
        seen.add(key)
        claims.append(claim)
        if len(claims) >= max_items:
            break
    return claims


def claim_key(claim: dict[str, Any]) -> tuple[str, str]:
    subject = re.sub(r"[\s，。；;,.!?！？:：\"'“”‘’]+", "", str(claim.get("subject") or ""))
    predicate = re.sub(
        r"[\s，。；;,.!?！？:：\"'“”‘’]+", "", str(claim.get("predicate") or "")
    )
    return subject.casefold(), predicate.casefold()


def _value_equal(left: str, right: str) -> bool:
    return (
        re.sub(r"[\s，。；;,.!?！？:：\"'“”‘’]+", "", left).casefold()
        == re.sub(r"[\s，。；;,.!?！？:：\"'“”‘’]+", "", right).casefold()
    )


def claims_conflict(left: dict[str, Any], right: dict[str, Any]) -> bool:
    if claim_key(left) != claim_key(right):
        return False
    if left.get("polarity") != right.get("polarity"):
        return True
    left_value = str(left.get("value") or "")
    right_value = str(right.get("value") or "")
    if left_value and right_value:
        return not _value_equal(left_value, right_value)
    return False


def claim_from_atom(atom: Any) -> dict[str, Any] | None:
    metadata = getattr(atom, "metadata", None)
    if not isinstance(metadata, dict):
        return None
    claim = metadata.get("claim")
    if not isinstance(claim, dict):
        return None
    return normalize_claim(claim) if claim.get("subject") else None


def claim_atom_type(claim: dict[str, Any]) -> AtomType:
    predicate = str(claim.get("predicate") or "")
    if any(word in predicate for word in _PREFERENCE_PREDICATES):
        return AtomType.PREFERENCE
    if any(word in predicate for word in _PLANNED_PREDICATES):
        return AtomType.PLANNED
    return AtomType.FACTUAL


def claim_to_atom(
    claim: dict[str, Any],
    *,
    parent_importance: float = 0.5,
    participants: list[str] | None = None,
    session_id: str | None = None,
    persona_id: str | None = None,
) -> MemoryAtom:
    content = "".join(
        [
            str(claim.get("subject") or ""),
            str(claim.get("predicate") or ""),
            str(claim.get("value") or ""),
        ]
    )
    entities = list(dict.fromkeys([claim["subject"], *(participants or [])]))
    return MemoryAtom(
        parent_memory_id=0,
        atom_type=claim_atom_type(claim),
        content=content,
        entities=entities[:8],
        importance=parent_importance,
        confidence=float(claim.get("confidence", 0.7)),
        session_id=session_id,
        persona_id=persona_id,
        metadata={"claim": claim, "claim_atom": True},
    )


__all__ = [
    "claim_atom_type",
    "claim_from_atom",
    "claim_key",
    "claim_to_atom",
    "claims_conflict",
    "normalize_claim",
    "normalize_claims",
]
