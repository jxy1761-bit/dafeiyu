"""语义冲突复核：高精度候选 + 可选 LLM 判定 + 可追溯的 supersede 链。

默认 dry-run，不自动改档；只有显式开启并关闭 dry-run 才会落库。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from astrbot.api import logger

from ..processors.claim_extraction import candidate_pairs
from ..processors.claim_utils import claim_from_atom, claim_key, claims_conflict
from ..utils import extract_json_from_response

_SYSTEM_PROMPT = (
    "你是记忆一致性审查员。只判断两条事实是否在说同一个对象/参数，以及它们的关系。"
    "输出严格 JSON，不要输出其他内容。"
)

_PROMPT = """比较下面两条事实。它们可能是在说同一件事的不同版本，也可能是无关内容。

事实 A（id={id_a}, 时间 {time_a}）：
{content_a}

事实 B（id={id_b}, 时间 {time_b}）：
{content_b}

只输出 JSON：
{{"relation":"duplicate|update|contradiction|unrelated","keep":"A|B","confidence":0.0,"reason":"一句话理由","evidence_a":"A 中的原文片段","evidence_b":"B 中的原文片段"}}

判定标准：
- duplicate：同一个事实的重复表述，没有新信息。
- update：B 更新了 A 的参数/状态，旧值不再适用。
- contradiction：两条事实互相矛盾，且无法判断哪条更新。
- unrelated：只是共享词/数字，不是同一件事。
- evidence 必须是各自原文里能逐字找到的片段；找不到就不要编。
"""


class ConflictReviewManager:
    def __init__(
        self,
        atom_store: Any,
        data_dir: str | Path,
        config: dict[str, Any] | None = None,
        llm_processor: Any = None,
    ):
        self.atom_store = atom_store
        self.data_dir = Path(data_dir)
        self.log_path = self.data_dir / "conflict_reviews.json"
        self.config = config or {}
        self.llm_processor = llm_processor
        self.enabled = bool(self.config.get("atom_conflict_review_enabled", False))
        self.dry_run = bool(self.config.get("atom_conflict_review_dry_run", True))
        self.max_pairs = max(1, int(self.config.get("atom_conflict_review_max_pairs", 10)))
        self.min_confidence = float(
            self.config.get("atom_conflict_review_min_confidence", 0.75)
        )
        self._reviews: dict[str, dict[str, Any]] = self._load()

    def set_llm_processor(self, processor: Any) -> None:
        self.llm_processor = processor

    async def review_once(self, dry_run: bool | None = None) -> dict[str, Any]:
        if not self.enabled:
            return {"enabled": False, "reviewed": 0, "applied": 0}
        if self.llm_processor is None:
            return {"enabled": True, "error": "llm_unavailable", "reviewed": 0}

        dry_run = self.dry_run if dry_run is None else bool(dry_run)
        atoms = await self.atom_store.list_active_atoms(
            ["factual", "preference", "relational", "planned"]
        )
        pairs = candidate_pairs(atoms, limit=self.max_pairs)
        reviewed = 0
        applied = 0
        decisions: list[dict[str, Any]] = []

        for left, right, candidate_reason in pairs:
            pair_key = f"{min(left.atom_id, right.atom_id)}:{max(left.atom_id, right.atom_id)}"
            if pair_key in self._reviews:
                continue
            decision = await self._judge(left, right)
            reviewed += 1
            entry = {
                "pair": pair_key,
                "candidate_reason": candidate_reason,
                "a_id": left.atom_id,
                "b_id": right.atom_id,
                "a": left.content[:160],
                "b": right.content[:160],
                "decision": decision,
                "dry_run": dry_run,
                "at": time.time(),
            }
            self._reviews[pair_key] = entry
            decisions.append(entry)
            if not decision:
                continue
            if decision["relation"] == "unrelated":
                continue
            if decision["confidence"] < self.min_confidence:
                continue
            if not dry_run:
                applied += await self._apply(left, right, decision, candidate_reason)
                entry["applied"] = True

        self._write()
        if reviewed:
            logger.info(
                f"[ConflictReview] 复核 {reviewed} 对，应用 {applied} 对"
                f"（dry_run={dry_run}）"
            )
        return {
            "enabled": True,
            "dry_run": dry_run,
            "candidates": len(pairs),
            "reviewed": reviewed,
            "applied": applied,
            "decisions": decisions,
        }

    async def _judge(self, left: Any, right: Any) -> dict[str, Any] | None:
        left_claim = claim_from_atom(left)
        right_claim = claim_from_atom(right)
        if (
            left_claim
            and right_claim
            and claim_key(left_claim) == claim_key(right_claim)
            and claims_conflict(left_claim, right_claim)
        ):
            # 结构化 claim 同主语+谓词但值/极性不同：不需要再让模型猜。
            newer = left if float(left.created_at) >= float(right.created_at) else right
            gap = abs(float(left.created_at) - float(right.created_at))
            relation = "contradiction" if gap < 60 else "update"
            return {
                "relation": relation,
                "keep": "A" if newer is left else "B",
                "confidence": 0.9,
                "reason": (
                    f"结构化 claim 冲突：{left_claim.get('value') or left_claim.get('polarity')}"
                    f" -> {right_claim.get('value') or right_claim.get('polarity')}"
                ),
                "evidence_a": left_claim.get("quote") or left.content[:80],
                "evidence_b": right_claim.get("quote") or right.content[:80],
            }

        prompt = _PROMPT.format(
            id_a=left.atom_id,
            time_a=time.strftime(
                "%Y-%m-%d %H:%M", time.localtime(float(left.created_at))
            ),
            content_a=left.content[:300],
            id_b=right.atom_id,
            time_b=time.strftime(
                "%Y-%m-%d %H:%M", time.localtime(float(right.created_at))
            ),
            content_b=right.content[:300],
        )
        try:
            raw = await self.llm_processor._call_llm_with_retry(
                prompt=prompt, system_prompt=_SYSTEM_PROMPT
            )
        except Exception as exc:
            logger.warning("[ConflictReview] LLM 判定失败: %s", exc)
            return None
        try:
            data = json.loads(extract_json_from_response(str(raw)))
        except (TypeError, ValueError, json.JSONDecodeError):
            logger.warning("[ConflictReview] 判定结果不是 JSON")
            return None
        if not isinstance(data, dict):
            return None

        relation = str(data.get("relation") or "").strip().lower()
        if relation not in {"duplicate", "update", "contradiction", "unrelated"}:
            return None
        keep = str(data.get("keep") or "").strip().upper()
        if keep not in {"A", "B"}:
            keep = ""
        try:
            confidence = float(data.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        confidence = max(0.0, min(1.0, confidence))

        evidence_a = str(data.get("evidence_a") or "").strip()
        evidence_b = str(data.get("evidence_b") or "").strip()
        if evidence_a and evidence_a not in left.content:
            confidence = max(0.0, confidence - 0.25)
            evidence_a = ""
        if evidence_b and evidence_b not in right.content:
            confidence = max(0.0, confidence - 0.25)
            evidence_b = ""

        return {
            "relation": relation,
            "keep": keep,
            "confidence": round(confidence, 3),
            "reason": str(data.get("reason") or "")[:200],
            "evidence_a": evidence_a[:120],
            "evidence_b": evidence_b[:120],
        }

    async def _apply(
        self,
        left: Any,
        right: Any,
        decision: dict[str, Any],
        candidate_reason: str,
    ) -> int:
        relation = decision["relation"]
        if relation == "duplicate":
            keep = left if (left.confidence, left.importance) >= (
                right.confidence,
                right.importance,
            ) else right
        else:
            # update / contradiction：以创建时间更新的一条为准。
            keep = left if float(left.created_at) >= float(right.created_at) else right
        old = right if keep is left else left
        if old.atom_id == keep.atom_id:
            return 0

        now = time.time()
        retired = await self.atom_store.mark_superseded(
            old.atom_id,
            by_atom_id=keep.atom_id,
            reason=f"conflict_review:{relation}",
            updates={
                "valid_until": now,
                "review_confidence": decision["confidence"],
                "review_reason": decision["reason"],
                "review_candidate": candidate_reason,
            },
        )
        if not retired:
            return 0
        await self.atom_store.update_metadata(
            keep.atom_id,
            {
                "supersedes_atom_ids": [old.atom_id],
                "conflict_group": candidate_reason.split(":")[0],
                "review_confidence": decision["confidence"],
                "review_reason": decision["reason"],
                "valid_from": now,
            },
        )
        return 1

    def _load(self) -> dict[str, dict[str, Any]]:
        try:
            payload = json.loads(self.log_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except Exception:
            return {}
        reviews = payload.get("reviews") if isinstance(payload, dict) else None
        return reviews if isinstance(reviews, dict) else {}

    def _write(self) -> None:
        payload = {"version": 1, "updated_at": time.time(), "reviews": self._reviews}
        tmp = self.log_path.with_suffix(".json.tmp")
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
            )
            tmp.replace(self.log_path)
        except Exception:
            logger.warning("[ConflictReview] 复核日志写入失败", exc_info=True)


__all__ = ["ConflictReviewManager"]
