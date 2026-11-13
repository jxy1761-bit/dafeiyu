"""主动找人的轻量 LLM 情境复核（默认 dry-run）。

只在确实引用真实素材时调用一次；模型只判断这条主动消息该不该发，
不负责生成内容。
"""

from __future__ import annotations

import json
import re
from typing import Any

_SYSTEM_PROMPT = (
    "你是主动消息的情境审查员。只输出 JSON，不要输出其他内容。"
)

_PROMPT = """现在考虑要不要让 AI 主动给主人发下面这条消息。
候选消息：{text}
引用的真实素材：{material}
主人最后说的话：{last_user_text}
已沉默：{silence} 分钟

从三个维度判断：
1. relevance：这条消息和当前关系/素材有关吗？
2. appropriateness：现在适合主动开口吗？主人在忙、在难受、在说正事时不适合。
3. naturalness：像不像随手发的一条消息？会不会显得刻意、像汇报或像在翻档案？

只输出 JSON：
{{"allow":true,"relevance":0.0,"appropriateness":0.0,"naturalness":0.0,"reason":"一句话"}}
拿不准时宁可 allow=false。不要因为“有素材”就允许硬提。
"""


def build_prompt(
    text: str,
    material_text: str,
    last_user_text: str,
    silence_minutes: int,
) -> str:
    return _PROMPT.format(
        text=str(text or "")[:200],
        material=str(material_text or "无")[:200],
        last_user_text=str(last_user_text or "无")[:120],
        silence=int(silence_minutes),
    )


def parse_response(raw: str) -> dict[str, Any] | None:
    text = str(raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    try:
        scores = {
            key: max(0.0, min(1.0, float(data.get(key, 0.0))))
            for key in ("relevance", "appropriateness", "naturalness")
        }
    except (TypeError, ValueError):
        return None
    return {
        "allow": bool(data.get("allow")),
        "relevance": scores["relevance"],
        "appropriateness": scores["appropriateness"],
        "naturalness": scores["naturalness"],
        "reason": str(data.get("reason") or "")[:160],
    }


def decide(
    decision: dict[str, Any] | None, min_score: float = 0.5
) -> tuple[bool, str]:
    if not decision:
        return True, "judge_unavailable"
    if not decision.get("allow"):
        return False, "llm_disallow"
    lowest = min(
        decision.get("relevance", 0),
        decision.get("appropriateness", 0),
        decision.get("naturalness", 0),
    )
    if lowest < min_score:
        return False, f"score_below_min:{lowest:.2f}"
    return True, "allowed"


async def judge(
    provider: Any,
    *,
    text: str,
    material_text: str,
    last_user_text: str,
    silence_minutes: int,
) -> dict[str, Any] | None:
    if provider is None:
        return None
    try:
        response = await provider.text_chat(
            prompt=build_prompt(text, material_text, last_user_text, silence_minutes),
            system_prompt=_SYSTEM_PROMPT,
        )
    except Exception:
        return None
    return parse_response(getattr(response, "completion_text", "") or "")


__all__ = ["build_prompt", "decide", "judge", "parse_response"]
