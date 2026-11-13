"""Periodic lifecycle manager for memory atoms."""

from __future__ import annotations

import asyncio
import re
import time
from collections import Counter, defaultdict
from typing import Any

from astrbot.api import logger

from ...storage.atom_store import AtomStore
from ..models.memory_atom import looks_like_stable_identity


def _content_tokens(content: str) -> set[str]:
    text = re.sub(r"\s+", "", str(content or ""))
    if len(text) < 2:
        return set()
    return {text[i : i + 2] for i in range(len(text) - 1)}


def _jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


class AtomLifecycleManager:
    """Schedule and execute atom lifecycle maintenance tasks."""

    def __init__(
        self,
        atom_store: AtomStore,
        config: dict[str, Any] | None = None,
    ):
        self.atom_store = atom_store
        self.config = config or {}
        self._maintenance_interval_hours = float(
            self.config.get("atom_maintenance_interval_hours", 24.0)
        )
        self._forget_delay_days = float(self.config.get("atom_forget_delay_days", 7.0))
        self._purge_delay_days = float(
            self.config.get(
                "atom_purge_delay_days",
                max(self._forget_delay_days * 4.0, 30.0),
            )
        )
        self._stable_enabled = bool(
            self.config.get("atom_stable_enabled", True)
        )
        self._duplicate_similarity = float(
            self.config.get("atom_duplicate_similarity", 0.6)
        )
        self._stable_ttl_days = float(
            self.config.get("atom_stable_ttl_days", 3650.0)
        )
        self._consolidation_max_atoms = int(
            self.config.get("atom_consolidation_max_atoms", 2000)
        )
        self._consolidation_max_comparisons = int(
            self.config.get("atom_consolidation_max_comparisons", 20000)
        )
        self._consolidation_time_budget = float(
            self.config.get("atom_consolidation_time_budget_seconds", 8.0)
        )
        self._running = False
        self._task: asyncio.Task | None = None
        self._conflict_review_manager: Any = None

    def set_conflict_review_manager(self, manager: Any) -> None:
        self._conflict_review_manager = manager

    async def start(self) -> None:
        """Begin periodic maintenance in the background."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._maintenance_loop())

    async def stop(self) -> None:
        """Cancel the maintenance loop."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _maintenance_loop(self) -> None:
        while self._running:
            try:
                await self.run_maintenance()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.error("[AtomLifecycle] 维护任务异常", exc_info=True)
                await asyncio.sleep(60.0)
                continue
            await asyncio.sleep(self._maintenance_interval_hours * 3600.0)

    async def run_maintenance(self) -> dict[str, int]:
        """Execute one full maintenance pass. Returns counts per action."""
        result: dict[str, int] = {}

        # 0. 巩固稳定事实并合并近重复原子（不删除证据）。
        if self._stable_enabled:
            result.update(await self.consolidate_active_atoms())

        # 0.5 语义冲突复核（默认关闭；dry-run 时不改档）。
        if self._conflict_review_manager is not None:
            try:
                review = await self._conflict_review_manager.review_once()
                if review.get("reviewed"):
                    result["conflict_reviewed"] = int(review.get("reviewed") or 0)
                    result["conflict_applied"] = int(review.get("applied") or 0)
            except Exception:
                logger.error("[AtomLifecycle] 冲突复核失败", exc_info=True)

        # 1. Expire stale atoms
        expired = await self.atom_store.expire_stale_atoms()
        result["expired"] = expired

        # 2. Soft-delete old expired atoms: keep metadata but remove them from FTS.
        forgotten = await self.atom_store.forget_expired_atoms(self._forget_delay_days)
        result["forgotten"] = forgotten

        # 3. Physically purge much older forgotten atoms to cap long-term storage.
        purged = await self.atom_store.cleanup_forgotten(self._purge_delay_days)
        result["purged"] = purged

        return result

    async def consolidate_active_atoms(self) -> dict[str, int]:
        """Promote durable facts and merge near-duplicate atoms.

        Only same persona + same atom type atoms are compared. Duplicates are
        marked superseded but keep their content for evidence; the canonical
        atom is marked stable with a long TTL.
        """
        atoms = await self.atom_store.list_active_atoms(
            ["factual", "preference", "relational"]
        )
        if not atoms:
            return {"stable": 0, "superseded": 0}

        atoms = atoms[: max(1, self._consolidation_max_atoms)]

        # 身份类事实直接升为稳定事实；文字重复的事实走相似度合并。
        stable_count = 0
        stable_ids: set[int] = set()
        for atom in atoms:
            if atom.metadata.get("stability") == "stable":
                continue
            if atom.atom_type.value != "factual":
                continue
            if not looks_like_stable_identity(atom.content):
                continue
            if float(atom.importance) < 0.7:
                continue
            updated = await self.atom_store.mark_stable(
                atom.atom_id,
                ttl_days=self._stable_ttl_days,
                updates={
                    "stability": "stable",
                    "stable_since": time.time(),
                    "stable_reason": "identity_fact",
                    "support_count": 1,
                },
            )
            if updated:
                stable_count += 1
                stable_ids.add(atom.atom_id)

        token_sets: list[set[str]] = []
        inverted: dict[str, list[int]] = defaultdict(list)
        for index, atom in enumerate(atoms):
            tokens = _content_tokens(atom.content)
            token_sets.append(tokens)
            for token in tokens:
                inverted[token].append(index)

        # 出现次数过多的 token 信息量低，而且会让候选组合爆炸；只保留
        # 2~60 次的 token 做倒排，再限制每个原子最多比较 40 个候选。
        usable_tokens = {
            token for token, postings in inverted.items() if 2 <= len(postings) <= 60
        }

        parent = list(range(len(atoms)))

        def find(index: int) -> int:
            while parent[index] != index:
                parent[index] = parent[parent[index]]
                index = parent[index]
            return index

        def union(left: int, right: int) -> None:
            root_left, root_right = find(left), find(right)
            if root_left != root_right:
                parent[root_right] = root_left

        deadline = time.monotonic() + max(1.0, self._consolidation_time_budget)
        comparisons = 0
        budget_hit = False
        for index, tokens in enumerate(token_sets):
            if budget_hit:
                break
            shared_counts: Counter[int] = Counter()
            for token in tokens:
                if token not in usable_tokens:
                    continue
                for other in inverted[token]:
                    if other > index:
                        shared_counts[other] += 1
            for other, shared in shared_counts.most_common(40):
                if shared < 3:
                    continue
                if comparisons >= self._consolidation_max_comparisons:
                    budget_hit = True
                    break
                if index % 200 == 0 and time.monotonic() > deadline:
                    budget_hit = True
                    break
                left, right = atoms[index], atoms[other]
                if left.atom_type != right.atom_type:
                    continue
                if (left.persona_id or "") != (right.persona_id or ""):
                    continue
                comparisons += 1
                if _jaccard(tokens, token_sets[other]) >= self._duplicate_similarity:
                    union(index, other)

        if budget_hit:
            logger.info(
                f"[AtomLifecycle] 巩固达到预算上限（比较 {comparisons} 次），"
                "本轮剩余候选留到下次"
            )

        clusters: dict[int, list[int]] = defaultdict(list)
        for index in range(len(atoms)):
            clusters[find(index)].append(index)

        superseded_count = 0
        for members in clusters.values():
            if len(members) < 2:
                continue
            canonical_index = max(
                members,
                key=lambda idx: (
                    int(atoms[idx].reinforcement_count),
                    float(atoms[idx].confidence),
                    float(atoms[idx].importance),
                    -float(atoms[idx].created_at),
                ),
            )
            canonical = atoms[canonical_index]
            others = [idx for idx in members if idx != canonical_index]
            supporting_ids = [atoms[idx].atom_id for idx in others]
            evidence_memory_ids = sorted(
                {atoms[idx].parent_memory_id for idx in members}
            )

            updated = await self.atom_store.mark_stable(
                canonical.atom_id,
                ttl_days=self._stable_ttl_days,
                updates={
                    "stability": "stable",
                    "stable_since": canonical.metadata.get(
                        "stable_since", time.time()
                    ),
                    "stable_reason": "repeated_fact",
                    "support_count": len(members),
                    "supporting_atom_ids": supporting_ids,
                    "evidence_memory_ids": evidence_memory_ids,
                },
            )
            if updated and canonical.atom_id not in stable_ids:
                stable_count += 1
                stable_ids.add(canonical.atom_id)

            for idx in others:
                other = atoms[idx]
                if other.metadata.get("stability") == "stable":
                    # 已经是稳定事实的重复项保留，避免把两条长期事实压成一条。
                    continue
                retired = await self.atom_store.mark_superseded(
                    other.atom_id,
                    by_atom_id=canonical.atom_id,
                    reason="near_duplicate",
                    updates={"consolidated_evidence_memory_ids": evidence_memory_ids},
                )
                if retired:
                    superseded_count += 1

        if stable_count or superseded_count:
            logger.info(
                f"[AtomLifecycle] 稳定事实 {stable_count} 条，合并重复 {superseded_count} 条"
            )
        return {"stable": stable_count, "superseded": superseded_count}

    async def run_manual_reinforcement(
        self,
        new_atoms: list,
        similarity_threshold: float = 0.6,
    ) -> int:
        """Attempt to find and reinforce existing atoms similar to new ones.

        Uses simple content-based overlap (Jaccard on token sets) for efficiency.
        Returns the number of atoms that were reinforced.
        """
        if not new_atoms:
            return 0

        reinforced = 0
        for new_atom in new_atoms:
            content = str(new_atom.content)
            new_tokens = set(content.lower().split())
            # For CJK or short text, use character bigrams as fallback tokens
            if len(new_tokens) < 3:
                chars = content.replace(" ", "")
                if len(chars) >= 4:
                    new_tokens = {chars[i : i + 2] for i in range(len(chars) - 1)}

            if len(new_tokens) < 2:
                continue

            search_query = " ".join(list(new_tokens)[:8])
            existing = await self.atom_store.search_fts(
                search_query,
                limit=5,
                include_expired=False,
            )
            for ex in existing:
                ex_content = ex.content.lower()
                ex_tokens = set(ex_content.split())
                if len(ex_tokens) < 2:
                    ex_tokens = (
                        {ex_content[i : i + 2] for i in range(len(ex_content) - 1)}
                        if len(ex_content) >= 4
                        else set()
                    )
                if not ex_tokens or not new_tokens:
                    continue
                jaccard = len(new_tokens & ex_tokens) / max(
                    1, len(new_tokens | ex_tokens)
                )
                if jaccard >= similarity_threshold:
                    await self.atom_store.reinforce(
                        ex.atom_id,
                        new_confidence=float(getattr(new_atom, "confidence", 0.7)),
                    )
                    reinforced += 1
                    break

        return reinforced


__all__ = ["AtomLifecycleManager"]
