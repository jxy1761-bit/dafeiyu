"""主动找人真实素材选择测试。"""

from __future__ import annotations

import sys
from pathlib import Path

PLUGINS_DIR = Path(__file__).resolve().parents[2]
if str(PLUGINS_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGINS_DIR))

from astrbot_plugin_dafeiyu_proactive_tg.material import (  # noqa: E402
    build_material_block,
    material_by_label,
    select_materials,
    sensitive_memory_ids,
    text_mentions_material,
)
from astrbot_plugin_dafeiyu_proactive_tg.main import (  # noqa: E402
    DEFAULTS,
    DafeiyuProactiveTg,
)
from astrbot_plugin_dafeiyu_proactive_tg.gate import (  # noqa: E402
    gate_candidate,
    is_distressed,
)


def _state():
    return {
        "personas": {
            "大肥鱼": {
                "open_loops": [
                    {
                        "text": "计划12月13日去看双子座烟花",
                        "date": "2026-12-13",
                        "memory_id": 22,
                        "importance": 0.8,
                    },
                    {
                        "text": "欠主人一顿饭",
                        "date": "2026-11-11",
                        "memory_id": 200,
                        "importance": 0.75,
                    },
                ],
                "recent_events": [
                    {
                        "text": "主人批评主动回复太刻意，我改完了",
                        "date": "2026-11-11",
                        "memory_id": 191,
                        "importance": 0.8,
                        "emotion": ["委屈"],
                    }
                ],
                "sensitive_topics": [
                    {"text": "不要提那件私事", "memory_id": 191},
                ],
            }
        }
    }


def test_select_materials_skips_sensitive_and_labels():
    state = _state()
    materials = select_materials(
        state,
        persona_id="大肥鱼",
        sensitive_ids=sensitive_memory_ids(state, "大肥鱼"),
        now=1794400000,
    )
    keys = [item["key"] for item in materials]
    assert "event:191" not in keys
    assert keys[0] == "loop:22"
    assert materials[0]["label"].startswith("L")
    assert any(item["key"] == "loop:200" for item in materials)


def test_select_materials_respects_cooldown_and_ignored_penalty():
    state = _state()
    now = 1794400000
    materials = select_materials(
        state,
        now=now,
        used={"loop:22": now - 3600},
        cooldown_seconds=7 * 86400,
        limit=3,
    )
    assert all(item["key"] != "loop:22" for item in materials)

    materials = select_materials(
        state,
        now=now,
        used={"loop:22": now - 8 * 86400},
        ignored={"loop:22": 1},
        cooldown_seconds=7 * 86400,
        limit=3,
    )
    assert all(item["key"] != "loop:22" for item in materials)


def test_material_block_and_overlap():
    block = build_material_block([])
    assert "没有可引用的真实素材" in block
    assert "不要描述" in block

    state = _state()
    materials = select_materials(state, now=1794400000, limit=2)
    block = build_material_block(materials)
    assert "[L1]" in block
    assert "MATERIAL" in block

    item = materials[0]
    assert text_mentions_material("我还记着那场双子座烟花呢", item) is True
    assert text_mentions_material("今天好困", item) is False

    assert material_by_label(materials, "L1") is item
    assert material_by_label(materials, "X9") is None


def test_parse_strips_material_line_and_keeps_tag():
    plugin = object.__new__(DafeiyuProactiveTg)
    plugin.cfg = dict(DEFAULTS)

    parsed = plugin._parse("SEND miss\n突然想戳你一下\nMATERIAL L1")
    assert parsed == ("miss", "突然想戳你一下", "L1")

    assert plugin._parse("SKIP") is None


def test_three_layer_gate_blocks_past_reference_without_material():
    allowed, layer, reason = gate_candidate(
        "上次你说想去西湖，我还记着呢", material_key=None
    )
    assert allowed is False
    assert layer == "relevance"
    assert reason == "past_reference_without_material"

    allowed, _, _ = gate_candidate("突然想戳你一下", material_key=None)
    assert allowed is True


def test_three_layer_gate_respects_user_distress():
    assert is_distressed("今天好烦，压力好大")
    allowed, layer, reason = gate_candidate(
        "还记得上次我们聊的烟花吗",
        material_key="loop:22",
        last_user_text="今天好烦，压力好大",
    )
    assert allowed is False
    assert layer == "appropriateness"
    assert reason == "user_distressed"

    allowed, _, _ = gate_candidate(
        "还记得上次我们聊的烟花吗",
        material_key="loop:22",
        last_user_text="今天加班",
    )
    assert allowed is True


def test_three_layer_gate_blocks_mechanical_wording():
    allowed, layer, reason = gate_candidate(
        "根据我的记忆，你之前提过这件事", material_key="loop:1"
    )
    assert allowed is False
    assert layer == "naturalness"
    assert reason == "mechanical_memory_wording"
