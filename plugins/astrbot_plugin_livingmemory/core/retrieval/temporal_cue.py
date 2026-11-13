"""中文时间线索解析：把“上周/上个月/去年/11月11日”变成可查询的日期范围。

只服务召回增强，不参与记忆写入，也不替模型推断事件时间。
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta


_RECALL_CUES = (
    "上次",
    "之前",
    "以前",
    "那次",
    "还记得",
    "什么时候",
    "哪天",
    "做了什么",
    "聊了什么",
    "发生了什么",
    "说过",
    "提过",
)

_ISO_DATE = re.compile(r"(20\d{2})-(\d{1,2})-(\d{1,2})")
_CN_DATE = re.compile(r"(20\d{2})年(\d{1,2})月(\d{1,2})日")
_MD_DATE = re.compile(r"(?<!\d)(\d{1,2})月(\d{1,2})日")
_CN_MONTH = re.compile(r"(?<!\d)(\d{1,2})月(?!\d)")


def has_recall_intent(query: str) -> bool:
    """只有明确在“翻旧账”时才启用时间通道，避免日常闲聊被日期带偏。"""
    text = str(query or "")
    return any(cue in text for cue in _RECALL_CUES)


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _week_range(today: date, offset_weeks: int = 0) -> tuple[date, date]:
    monday = today - timedelta(days=today.weekday())
    start = monday - timedelta(weeks=offset_weeks)
    return start, start + timedelta(days=6)


def _last_month_range(today: date) -> tuple[date, date]:
    first_this = today.replace(day=1)
    last_prev = first_this - timedelta(days=1)
    return last_prev.replace(day=1), last_prev


def parse_temporal_range(
    query: str, now: datetime | None = None
) -> tuple[str, str, str] | None:
    """返回 (date_start, date_end, label)；没有明确时间线索时返回 None。"""
    text = str(query or "")
    if not has_recall_intent(text):
        return None

    today = (now or datetime.now()).date()

    match = _ISO_DATE.search(text)
    if match:
        value = _safe_date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        if value:
            return value.isoformat(), value.isoformat(), "指定日期"

    match = _CN_DATE.search(text)
    if match:
        value = _safe_date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        if value:
            return value.isoformat(), value.isoformat(), "指定日期"

    match = _MD_DATE.search(text)
    if match:
        value = _safe_date(today.year, int(match.group(1)), int(match.group(2)))
        if value:
            return value.isoformat(), value.isoformat(), "指定日期"

    if "昨天" in text:
        value = today - timedelta(days=1)
        return value.isoformat(), value.isoformat(), "昨天"
    if "前天" in text:
        value = today - timedelta(days=2)
        return value.isoformat(), value.isoformat(), "前天"
    if "今天" in text:
        return today.isoformat(), today.isoformat(), "今天"

    if "上上个月" in text:
        first_this = today.replace(day=1)
        prev_last = first_this - timedelta(days=1)
        start = (prev_last.replace(day=1) - timedelta(days=1)).replace(day=1)
        end = prev_last.replace(day=1) - timedelta(days=1)
        return start.isoformat(), end.isoformat(), "上上个月"

    if "上个月" in text or "上月" in text:
        start, end = _last_month_range(today)
        return start.isoformat(), end.isoformat(), "上个月"

    if "这个月" in text or "本月" in text:
        return today.replace(day=1).isoformat(), today.isoformat(), "这个月"

    if "上上周" in text:
        start, end = _week_range(today, 2)
        return start.isoformat(), end.isoformat(), "上上周"

    if "上周" in text:
        start, end = _week_range(today, 1)
        return start.isoformat(), end.isoformat(), "上周"

    if "这周" in text or "本周" in text:
        start, _ = _week_range(today, 0)
        return start.isoformat(), today.isoformat(), "这周"

    if "周末" in text:
        monday = today - timedelta(days=today.weekday())
        saturday = monday + timedelta(days=5)
        if saturday > today:
            saturday -= timedelta(weeks=1)
        return saturday.isoformat(), (saturday + timedelta(days=1)).isoformat(), "周末"

    if "去年" in text:
        year = today.year - 1
        return f"{year}-01-01", f"{year}-12-31", "去年"

    match = _CN_MONTH.search(text)
    if match:
        month = int(match.group(1))
        if 1 <= month <= 12:
            start = date(today.year, month, 1)
            if month == 12:
                end = date(today.year, 12, 31)
            else:
                end = date(today.year, month + 1, 1) - timedelta(days=1)
            return start.isoformat(), end.isoformat(), f"{month}月"

    return None


__all__ = ["has_recall_intent", "parse_temporal_range"]
