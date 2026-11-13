"""主动找人的三层门控：相关性 / 情境适宜 / 表达自然。

只做确定性的硬门控，不额外调用模型；模型仍负责“想不想找他”。
"""

from __future__ import annotations

PAST_REFERENCE_MARKERS = (
    "上次",
    "之前",
    "以前",
    "那次",
    "还记得",
    "你说过",
    "你提过",
    "记得你",
)
DISTRESS_MARKERS = (
    "难过",
    "伤心",
    "烦",
    "累",
    "崩溃",
    "生气",
    "吵架",
    "哭",
    "不想活",
    "难受",
    "压力",
    "焦虑",
    "失眠",
    "疼",
)
MECHANICAL_MARKERS = (
    "根据我的记忆",
    "根据档案",
    "数据库显示",
    "系统记录",
    "我的记忆里",
)


def is_distressed(text: str) -> bool:
    value = str(text or "")
    return any(marker in value for marker in DISTRESS_MARKERS)


def gate_candidate(
    text: str,
    *,
    material_key: str | None,
    last_user_text: str = "",
) -> tuple[bool, str, str]:
    """返回 (allowed, layer, reason)。

    - 相关性：没有真实素材时，不许出现“上次/之前/那次”这类旧事指代。
    - 情境适宜：主人刚表达负面情绪时，不主动翻旧事。
    - 表达自然：不许用“根据我的记忆/数据库”这种机器口径。
    """
    value = str(text or "").strip()
    has_past_reference = any(marker in value for marker in PAST_REFERENCE_MARKERS)
    mechanical = any(marker in value for marker in MECHANICAL_MARKERS)

    if mechanical:
        return False, "naturalness", "mechanical_memory_wording"

    if material_key:
        if is_distressed(last_user_text):
            return False, "appropriateness", "user_distressed"
        return True, "", ""

    if has_past_reference:
        return False, "relevance", "past_reference_without_material"

    return True, "", ""


__all__ = [
    "DISTRESS_MARKERS",
    "PAST_REFERENCE_MARKERS",
    "gate_candidate",
    "is_distressed",
]
