"""主动找人 LLM 情境门控测试（不发真实请求）。"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

PLUGINS_DIR = Path(__file__).resolve().parents[2]
if str(PLUGINS_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGINS_DIR))

from astrbot_plugin_dafeiyu_proactive_tg.llm_gate import (  # noqa: E402
    build_prompt,
    decide,
    judge,
    parse_response,
)


def test_parse_and_decide_gate():
    decision = parse_response(
        json.dumps(
            {
                "allow": True,
                "relevance": 0.8,
                "appropriateness": 0.7,
                "naturalness": 0.9,
                "reason": "自然",
            }
        )
    )
    assert decision["allow"] is True
    allowed, reason = decide(decision, 0.5)
    assert allowed is True
    assert reason == "allowed"

    low = parse_response(
        json.dumps(
            {
                "allow": True,
                "relevance": 0.8,
                "appropriateness": 0.2,
                "naturalness": 0.9,
            }
        )
    )
    allowed, reason = decide(low, 0.5)
    assert allowed is False
    assert reason.startswith("score_below_min")

    assert decide(None, 0.5) == (True, "judge_unavailable")


def test_build_prompt_contains_context():
    prompt = build_prompt("突然想起烟花", "12月13日看烟花", "今天加班", 120)
    assert "突然想起烟花" in prompt
    assert "12月13日看烟花" in prompt
    assert "今天加班" in prompt
    assert "120" in prompt


def test_judge_uses_provider_without_real_network():
    class FakeProvider:
        async def text_chat(self, prompt: str, system_prompt: str):
            return SimpleNamespace(
                completion_text=json.dumps(
                    {
                        "allow": False,
                        "relevance": 0.9,
                        "appropriateness": 0.2,
                        "naturalness": 0.8,
                        "reason": "主人在忙",
                    }
                )
            )

    decision = asyncio.run(
        judge(
            FakeProvider(),
            text="还记得烟花吗",
            material_text="12月13日看烟花",
            last_user_text="今天加班",
            silence_minutes=120,
        )
    )
    assert decision["allow"] is False
    assert decision["appropriateness"] == 0.2
