"""从原子文本里抽取“参数型声明”，为冲突复核提供高精度候选。

只处理带配置关键词、纯中文主语、数字值的声明；不做通用语义理解。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable

from .claim_utils import (
    claim_from_atom,
    claim_key,
    claims_conflict as structured_claims_conflict,
)

CONFIG_KEYWORDS = (
    "概率",
    "频率",
    "间隔",
    "上限",
    "下限",
    "显存",
    "内存",
    "档",
    "分钟",
    "小时",
    "条",
    "次",
    "步",
    "岁",
    "体重",
    "价格",
    "预算",
    "token",
    "模型",
    "版本",
    "端口",
    "权重",
    "公里",
    "GB",
    "MB",
    "配置",
    "参数",
    "设定",
    "设置",
)

_FILLERS = (
    "已经",
    "目前",
    "现在",
    "之前",
    "后来",
    "当时",
    "刚刚",
    "原来",
    "其实",
    "从",
    "改成",
    "改为",
    "变成",
    "是",
    "为",
    "的",
    "了",
    "在",
    "有",
    "把",
    "将",
    "和",
    "跟",
    "今天",
    "昨天",
    "凌晨",
    "早上",
    "晚上",
    "今晚",
    "今年",
    "计划",
    "约",
)
_GENERIC_SUBJECTS = {"计划", "今晚", "今年", "今天", "当时", "现在", "目前"}
_NUMBER_RE = re.compile(r"(?<![\d\-.:/])(\d+(?:\.\d+)?)(?![\d\-.:/])")
_DATE_LIKE = re.compile(r"20\d{2}[年\-/]\d{1,2}|\d{1,2}月\d{1,2}日|\d{1,2}:\d{2}")
_NEGATIONS = ("不", "没", "无", "未", "取消", "停止", "关闭")


@dataclass(frozen=True)
class NumericClaim:
    subject: str
    value: str
    unit: str
    polarity: str  # "positive" / "negative"
    raw: str

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.subject, self.unit, self.polarity)


def _normalize_subject(value: str) -> str:
    text = re.sub(r"[^\u4e00-\u9fa5]", "", value)
    for filler in sorted(_FILLERS, key=len, reverse=True):
        text = text.replace(filler, "")
    return text[-10:]


def is_config_like(text: str) -> bool:
    lowered = str(text or "").lower()
    return any(keyword.lower() in lowered for keyword in CONFIG_KEYWORDS)


def extract_numeric_claims(text: str) -> list[NumericClaim]:
    content = str(text or "")
    if not content or not is_config_like(content):
        return []

    claims: list[NumericClaim] = []
    for match in _NUMBER_RE.finditer(content):
        value = match.group(1)
        if len(value) >= 6 or re.fullmatch(r"20\d{2}", value):
            continue
        window = content[max(0, match.start() - 16) : match.end() + 4]
        if _DATE_LIKE.search(window):
            continue

        before = content[max(0, match.start() - 20) : match.start()]
        subject = _normalize_subject(before)
        if len(subject) < 2 or subject in _GENERIC_SUBJECTS:
            continue
        if not re.fullmatch(r"[\u4e00-\u9fa5]{2,10}", subject):
            continue

        unit_match = re.match(
            r"\s*(%|分钟|小时|天|次|条|GB|MB|G|T|元|块|公里|km|步|个|岁|点|档|级)",
            content[match.end() :],
        )
        unit = unit_match.group(1) if unit_match else ""
        polarity = (
            "negative"
            if any(marker in before[-6:] for marker in _NEGATIONS)
            else "positive"
        )
        claims.append(
            NumericClaim(
                subject=subject,
                value=value,
                unit=unit,
                polarity=polarity,
                raw=content[max(0, match.start() - 10) : match.end() + len(unit)],
            )
        )
    return claims


def claims_conflict(left: NumericClaim, right: NumericClaim) -> bool:
    if left.subject != right.subject:
        return False
    if left.unit != right.unit:
        return False
    if left.value != right.value:
        return True
    return left.polarity != right.polarity


def candidate_pairs(
    atoms: Iterable[Any], limit: int = 20, max_atoms: int = 2000
) -> list[tuple[Any, Any, str]]:
    """返回 (atom_a, atom_b, reason) 的高精度候选对。

    优先使用总结阶段产出的结构化 claim（subject/predicate/value/polarity），
    没有 claim 的旧原子再退回参数型数字抽取。
    """
    atom_list = list(atoms)[:max_atoms]
    pairs: list[tuple[Any, Any, str]] = []
    seen: set[tuple[int, int]] = set()
    claimed_ids: set[int] = set()

    claim_groups: dict[
        tuple[str, str, str], list[tuple[Any, dict[str, Any]]]
    ] = {}
    for atom in atom_list:
        claim = claim_from_atom(atom)
        if not claim:
            continue
        atom_id = int(getattr(atom, "atom_id", 0) or 0)
        claimed_ids.add(atom_id)
        persona = str(getattr(atom, "persona_id", None) or "")
        subject, predicate = claim_key(claim)
        claim_groups.setdefault((persona, subject, predicate), []).append(
            (atom, claim)
        )

    for items in claim_groups.values():
        for index, (left_atom, left_claim) in enumerate(items):
            for right_atom, right_claim in items[index + 1 :]:
                if not structured_claims_conflict(left_claim, right_claim):
                    continue
                pair_key = tuple(
                    sorted(
                        (
                            int(getattr(left_atom, "atom_id", 0) or 0),
                            int(getattr(right_atom, "atom_id", 0) or 0),
                        )
                    )
                )
                if pair_key in seen:
                    continue
                seen.add(pair_key)
                pairs.append(
                    (
                        left_atom,
                        right_atom,
                        f"{left_claim['subject']}{left_claim['predicate']}: "
                        f"{left_claim.get('value') or left_claim.get('polarity')} -> "
                        f"{right_claim.get('value') or right_claim.get('polarity')}",
                    )
                )
                if len(pairs) >= limit:
                    return pairs

    groups: dict[
        tuple[str, str], dict[int, tuple[Any, list[NumericClaim]]]
    ] = {}
    for atom in atom_list:
        atom_id = int(getattr(atom, "atom_id", 0) or 0)
        if atom_id in claimed_ids:
            continue
        claims = extract_numeric_claims(getattr(atom, "content", ""))
        if not claims:
            continue
        by_key: dict[tuple[str, str], list[NumericClaim]] = {}
        for claim in claims:
            by_key.setdefault((claim.subject, claim.unit), []).append(claim)
        for key, key_claims in by_key.items():
            groups.setdefault(key, {})[atom_id] = (atom, key_claims)

    for (subject, unit), items in groups.items():
        members = list(items.values())
        for index, (left_atom, left_claims) in enumerate(members):
            for right_atom, right_claims in members[index + 1 :]:
                if str(getattr(left_atom, "atom_type", "")) != str(
                    getattr(right_atom, "atom_type", "")
                ):
                    continue
                if (getattr(left_atom, "persona_id", None) or "") != (
                    getattr(right_atom, "persona_id", None) or ""
                ):
                    continue
                pair_key = tuple(
                    sorted(
                        (
                            int(getattr(left_atom, "atom_id", 0) or 0),
                            int(getattr(right_atom, "atom_id", 0) or 0),
                        )
                    )
                )
                if pair_key in seen:
                    continue
                if any(
                    claims_conflict(left, right)
                    for left in left_claims
                    for right in right_claims
                ):
                    seen.add(pair_key)
                    pairs.append(
                        (
                            left_atom,
                            right_atom,
                            f"{subject}: "
                            f"{left_claims[0].value}{unit} -> "
                            f"{right_claims[0].value}{unit}",
                        )
                    )
                    if len(pairs) >= limit:
                        return pairs
    return pairs


__all__ = [
    "CONFIG_KEYWORDS",
    "NumericClaim",
    "candidate_pairs",
    "claims_conflict",
    "extract_numeric_claims",
    "is_config_like",
]
