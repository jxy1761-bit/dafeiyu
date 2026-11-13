"""主动找人的真实素材选择。

素材只来自 LivingMemory 生成的关系状态文件；没有素材时明确告诉模型
“不要描述具体共同经历”，从源头堵住编生活。
"""

from __future__ import annotations

import json
import re
import time
from datetime import date
from pathlib import Path
from typing import Any

_COMPLETED_MARKERS = ("作废", "取消", "已经", "已完成", "完成", "结束", "违背了")


def load_relationship_state(path: str | Path) -> dict[str, Any]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _persona_state(
    state: dict[str, Any], persona_id: str | None = None
) -> dict[str, Any]:
    personas = state.get("personas")
    if not isinstance(personas, dict):
        return {}
    if persona_id and isinstance(personas.get(persona_id), dict):
        return personas[persona_id]
    for value in personas.values():
        if isinstance(value, dict):
            return value
    return {}


def _normalize(text: Any, limit: int = 90) -> str:
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    return value[:limit] + ("…" if len(value) > limit else "")


def sensitive_memory_ids(
    state: dict[str, Any], persona_id: str | None = None
) -> set[int]:
    persona = _persona_state(state, persona_id)
    ids: set[int] = set()
    for item in persona.get("sensitive_topics") or []:
        if not isinstance(item, dict):
            continue
        memory_id = item.get("memory_id")
        if isinstance(memory_id, int):
            ids.add(memory_id)
    return ids


def _material_key(item: dict[str, Any], prefix: str) -> str:
    memory_id = item.get("memory_id")
    if memory_id is not None:
        return f"{prefix}:{memory_id}"
    return f"{prefix}:{abs(hash(_normalize(item.get('text'), 40)))}"


def select_materials(
    state: dict[str, Any],
    *,
    persona_id: str | None = None,
    used: dict[str, float] | None = None,
    ignored: dict[str, int] | None = None,
    sensitive_ids: set[int] | None = None,
    now: float | None = None,
    cooldown_seconds: float = 7 * 86400,
    limit: int = 2,
) -> list[dict[str, Any]]:
    """挑出仍可自然提起的真实素材；用过的按冷却时间过滤。"""
    now = time.time() if now is None else now
    used = used or {}
    ignored = ignored or {}
    sensitive_ids = sensitive_ids or set()
    persona = _persona_state(state, persona_id)
    if not persona:
        return []

    candidates: list[dict[str, Any]] = []
    for item in persona.get("open_loops") or []:
        if not isinstance(item, dict):
            continue
        candidates.append(
            {
                "key": _material_key(item, "loop"),
                "kind": "loop",
                "text": _normalize(item.get("text")),
                "date": str(item.get("date") or ""),
                "memory_id": item.get("memory_id"),
                "importance": float(item.get("importance") or 0.6),
            }
        )
    for item in persona.get("recent_events") or []:
        if not isinstance(item, dict):
            continue
        candidates.append(
            {
                "key": _material_key(item, "event"),
                "kind": "event",
                "text": _normalize(item.get("text")),
                "date": str(item.get("date") or ""),
                "memory_id": item.get("memory_id"),
                "importance": float(item.get("importance") or 0.6),
                "emotion": item.get("emotion") or [],
            }
        )

    selected: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in candidates:
        text = item["text"]
        if not text:
            continue
        if any(marker in text for marker in _COMPLETED_MARKERS):
            continue
        signature = re.sub(r"\W+", "", text)[:24]
        if not signature or signature in seen:
            continue
        if item.get("memory_id") in sensitive_ids:
            continue
        last_used = float(used.get(item["key"]) or 0)
        penalty = 1 + int(ignored.get(item["key"]) or 0)
        if now - last_used < cooldown_seconds * penalty:
            continue
        seen.add(signature)
        selected.append(item)

    today = date.today().isoformat()
    selected.sort(
        key=lambda item: (
            0 if item["date"] and item["date"] >= today else 1,
            item["kind"] != "loop",
            -item["importance"],
            item["date"] or "",
        ),
    )
    counters = {"loop": 0, "event": 0}
    for item in selected:
        counters[item["kind"]] += 1
        item["label"] = (
            f"L{counters['loop']}" if item["kind"] == "loop" else f"E{counters['event']}"
        )
    return selected[: max(1, limit)]


def build_material_block(materials: list[dict[str, Any]]) -> str:
    if not materials:
        return (
            "这次没有可引用的真实素材：不要描述任何具体发生过的共同经历，"
            "不要补充时间、地点或细节；只能表达想念，或说说你自己的状态。"
        )

    lines = [
        "可引用的真实素材（最多自然提一件；没感觉就别提，不要硬提）：",
    ]
    for item in materials:
        date = f"{item['date']} " if item.get("date") else ""
        kind = "未完成" if item["kind"] == "loop" else "近期重要"
        emotion = ""
        if item.get("emotion"):
            emotion = "，情绪：" + "、".join(str(value) for value in item["emotion"][:2])
        lines.append(f"- [{item['label']}] {date}{item['text']}（{kind}{emotion}）")
    lines.append(
        "提到的细节必须来自素材原文，不许补充、改写或编造。"
        "如果确实用了某条，在最后单独一行写：MATERIAL <编号>。"
    )
    return "\n".join(lines)


def material_by_label(
    materials: list[dict[str, Any]], label: str | None
) -> dict[str, Any] | None:
    if not label:
        return None
    wanted = str(label).strip().upper()
    for item in materials:
        if str(item.get("label") or "").upper() == wanted:
            return item
    return None


def text_mentions_material(text: str, material: dict[str, Any], window: int = 4) -> bool:
    """兜底判断：模型没写 MATERIAL 行时，看正文有没有素材的连续片段。"""
    source = re.sub(r"\s+", "", str(material.get("text") or ""))
    target = re.sub(r"\s+", "", str(text or ""))
    if len(source) < window or not target:
        return False
    for index in range(0, len(source) - window + 1):
        chunk = source[index : index + window]
        if chunk in target:
            return True
    return False


__all__ = [
    "build_material_block",
    "load_relationship_state",
    "material_by_label",
    "select_materials",
    "sensitive_memory_ids",
    "text_mentions_material",
]
