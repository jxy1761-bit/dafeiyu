"""Phase G：参数声明抽取与冲突复核测试。"""

from __future__ import annotations

import asyncio
import json

from astrbot_plugin_livingmemory.core.managers.conflict_review_manager import (
    ConflictReviewManager,
)
from astrbot_plugin_livingmemory.core.models.memory_atom import AtomType, MemoryAtom
from astrbot_plugin_livingmemory.core.processors.claim_extraction import (
    candidate_pairs,
    extract_numeric_claims,
)
from astrbot_plugin_livingmemory.core.processors.memory_processor import MemoryProcessor
from astrbot_plugin_livingmemory.storage.atom_store import AtomStore


def _atom(atom_id: int, content: str) -> MemoryAtom:
    atom = MemoryAtom(
        parent_memory_id=atom_id,
        atom_type=AtomType.FACTUAL,
        content=content,
        importance=0.8,
        confidence=0.8,
        persona_id="大肥鱼",
    )
    atom.atom_id = atom_id
    return atom


class _FakeLLM:
    def __init__(self, payload: dict):
        self.payload = payload

    async def _call_llm_with_retry(self, prompt: str, system_prompt: str):
        return json.dumps(self.payload, ensure_ascii=False)


def test_extract_numeric_claims_filters_dates_and_ids():
    claims = extract_numeric_claims("主动找人基础概率从0.35改成0.55")
    values = {(item.subject, item.value) for item in claims}
    assert any(subject.endswith("基础概率") and value == "0.35" for subject, value in values)
    assert any(subject.endswith("基础概率") and value == "0.55" for subject, value in values)

    claims = extract_numeric_claims("显存上限是16GB")
    assert any(item.subject == "显存上限" and item.value == "16" for item in claims)

    assert extract_numeric_claims("example-user于2026-11-09凌晨1点来查面板") == []
    assert extract_numeric_claims("example-user的QQ号是10000000") == []


def test_candidate_pairs_finds_parameter_updates_only():
    left = _atom(1, "显存上限是16GB")
    right = _atom(2, "显存上限是12GB")
    date_a = _atom(3, "2026-11-09 04:14 测试了语音")
    date_b = _atom(4, "2026-11-11 04:27 关闭了语音")
    unrelated = _atom(5, "今天骑了21公里")

    pairs = candidate_pairs([left, right, date_a, date_b, unrelated])
    assert len(pairs) == 1
    ids = {pairs[0][0].atom_id, pairs[0][1].atom_id}
    assert ids == {1, 2}


def test_claim_is_attached_to_matching_atom():
    processor = MemoryProcessor(llm_provider=None, context=None)
    metadata = {
        "key_facts": ["显存上限是16GB"],
        "topics": ["硬件"],
        "claims": [
            {
                "subject": "显存上限",
                "predicate": "是",
                "value": "16GB",
                "polarity": "positive",
                "quote": "显存上限是16GB",
                "confidence": 0.9,
            }
        ],
    }
    atoms = processor.classify_atoms_from_metadata(metadata, parent_importance=0.8)
    assert atoms
    assert any((atom.metadata or {}).get("claim") for atom in atoms)


def test_filtered_claim_creates_claim_atom():
    processor = MemoryProcessor(llm_provider=None, context=None)
    metadata = {
        "key_facts": ["今天去看了电影"],
        "topics": ["电影"],
        "claims": [
            {
                "subject": "夜鹿乐队",
                "predicate": "喜欢",
                "value": "《阿尔吉侬》",
                "polarity": "positive",
                "quote": "我喜欢《阿尔吉侬》",
                "confidence": 0.9,
            }
        ],
    }
    atoms = processor.classify_atoms_from_metadata(metadata, parent_importance=0.8)
    claim_atoms = [
        atom for atom in atoms if (atom.metadata or {}).get("claim_atom")
    ]
    assert len(claim_atoms) == 1
    assert claim_atoms[0].atom_type == AtomType.PREFERENCE
    assert "阿尔吉侬" in claim_atoms[0].content


def test_structured_claims_conflict_without_llm(tmp_path):
    class _ExplodingLLM:
        async def _call_llm_with_retry(self, prompt: str, system_prompt: str):
            raise AssertionError("structured claims must not call the LLM")

    async def scenario():
        store = AtomStore(str(tmp_path / "lm.db"))
        await store.initialize()
        left = MemoryAtom(
            parent_memory_id=1,
            atom_type=AtomType.PREFERENCE,
            content="喜欢《阿尔吉侬》",
            importance=0.8,
            confidence=0.8,
            persona_id="大肥鱼",
            metadata={
                "claim": {
                    "subject": "夜鹿乐队",
                    "predicate": "喜欢",
                    "value": "《阿尔吉侬》",
                    "polarity": "positive",
                    "quote": "喜欢《阿尔吉侬》",
                    "confidence": 0.9,
                }
            },
        )
        right = MemoryAtom(
            parent_memory_id=2,
            atom_type=AtomType.PREFERENCE,
            content="讨厌《阿尔吉侬》",
            importance=0.8,
            confidence=0.8,
            persona_id="大肥鱼",
            metadata={
                "claim": {
                    "subject": "夜鹿乐队",
                    "predicate": "喜欢",
                    "value": "《阿尔吉侬》",
                    "polarity": "negative",
                    "quote": "讨厌《阿尔吉侬》",
                    "confidence": 0.9,
                }
            },
        )
        left_id = await store.insert(left)
        right_id = await store.insert(right)
        manager = ConflictReviewManager(
            store,
            str(tmp_path / "data"),
            {
                "atom_conflict_review_enabled": True,
                "atom_conflict_review_dry_run": False,
            },
            _ExplodingLLM(),
        )
        result = await manager.review_once()
        atoms = {
            left_id: await store.get(left_id),
            right_id: await store.get(right_id),
        }
        return result, atoms

    result, atoms = asyncio.run(scenario())
    assert result["applied"] == 1
    assert sorted(atom.status.value for atom in atoms.values()) == [
        "active",
        "superseded",
    ]


def test_conflict_review_dry_run_does_not_mutate(tmp_path):
    async def scenario():
        store = AtomStore(str(tmp_path / "lm.db"))
        await store.initialize()
        first = await store.insert(
            MemoryAtom(
                parent_memory_id=1,
                atom_type=AtomType.FACTUAL,
                content="显存上限是16GB",
                importance=0.8,
                confidence=0.8,
                persona_id="大肥鱼",
            )
        )
        second = await store.insert(
            MemoryAtom(
                parent_memory_id=2,
                atom_type=AtomType.FACTUAL,
                content="显存上限是12GB",
                importance=0.8,
                confidence=0.8,
                persona_id="大肥鱼",
            )
        )
        manager = ConflictReviewManager(
            store,
            str(tmp_path / "data"),
            {
                "atom_conflict_review_enabled": True,
                "atom_conflict_review_dry_run": True,
            },
            _FakeLLM(
                {
                    "relation": "update",
                    "keep": "B",
                    "confidence": 0.9,
                    "reason": "显存上限被更新",
                    "evidence_a": "显存上限是16GB",
                    "evidence_b": "显存上限是12GB",
                }
            ),
        )
        result = await manager.review_once()
        first_atom = await store.get(first)
        second_atom = await store.get(second)
        return result, first_atom, second_atom

    result, first_atom, second_atom = asyncio.run(scenario())
    assert result["reviewed"] == 1
    assert result["applied"] == 0
    assert first_atom.status.value == "active"
    assert second_atom.status.value == "active"


def test_conflict_review_applies_supersede_when_enabled(tmp_path):
    async def scenario():
        store = AtomStore(str(tmp_path / "lm.db"))
        await store.initialize()
        first = await store.insert(
            MemoryAtom(
                parent_memory_id=1,
                atom_type=AtomType.FACTUAL,
                content="显存上限是16GB",
                importance=0.8,
                confidence=0.8,
                persona_id="大肥鱼",
            )
        )
        second = await store.insert(
            MemoryAtom(
                parent_memory_id=2,
                atom_type=AtomType.FACTUAL,
                content="显存上限是12GB",
                importance=0.8,
                confidence=0.8,
                persona_id="大肥鱼",
            )
        )
        manager = ConflictReviewManager(
            store,
            str(tmp_path / "data_apply"),
            {
                "atom_conflict_review_enabled": True,
                "atom_conflict_review_dry_run": False,
            },
            _FakeLLM(
                {
                    "relation": "update",
                    "keep": "B",
                    "confidence": 0.9,
                    "reason": "显存上限被更新",
                    "evidence_a": "显存上限是16GB",
                    "evidence_b": "显存上限是12GB",
                }
            ),
        )
        result = await manager.review_once()
        atoms = {first: await store.get(first), second: await store.get(second)}
        return result, atoms

    result, atoms = asyncio.run(scenario())
    assert result["applied"] == 1
    statuses = sorted(atom.status.value for atom in atoms.values())
    assert statuses == ["active", "superseded"]
    superseded = next(atom for atom in atoms.values() if atom.status.value == "superseded")
    assert superseded.metadata["superseded_by_atom_id"] in atoms
    assert superseded.metadata["review_confidence"] == 0.9
