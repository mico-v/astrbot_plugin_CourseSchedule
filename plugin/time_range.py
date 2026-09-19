"""Shared time-range parsing for find, the rank board and the day views.

``_parse_time_range`` understands the whole range vocabulary the chat commands
advertise: relative days, weeks and months, a single day, and
``YYYY-MM-DD..YYYY-MM-DD`` (also spelled with 至/到/~/— or extra spaces around
the separator).  ``find``'s ``time_range`` argument and the 上课时长榜 window
both go through it, so the two features can never disagree about what 本周
means.
"""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta

from .constants import LOCAL_TZ

_RANGE_SPLIT_RE = re.compile(r"\s*(?:\.\.|~|至|到|—|-{2,}|\bto\b)\s*", re.IGNORECASE)

_SINGLE_DAY_TOKENS = {
    "": 0,
    "today": 0,
    "今天": 0,
    "今日": 0,
    "tomorrow": 1,
    "明天": 1,
    "明日": 1,
    "yesterday": -1,
    "昨天": -1,
    "昨日": -1,
}

_THIS_WEEK = {"thisweek", "currentweek", "week", "本周", "这周", "这一周"}
_NEXT_WEEK = {"nextweek", "下周", "下一周"}
_LAST_WEEK = {"lastweek", "prevweek", "上周", "上一周"}
_THIS_MONTH = {"thismonth", "currentmonth", "month", "本月", "这个月"}
_NEXT_MONTH = {"nextmonth", "下月", "下个月"}
_LAST_MONTH = {"lastmonth", "prevmonth", "上月", "上个月"}

_RANGE_HINT = (
    "无法识别时间范围“{value}”，请使用 今日、本周、上周、本月、上月、下月，"
    "或 2026-09-01..2026-09-30 这样的日期范围。"
)


def _month_bounds(day: date, offset: int = 0) -> tuple[date, date]:
    """First and last day of the month ``offset`` months away from ``day``."""
    month_index = day.month - 1 + offset
    year = day.year + month_index // 12
    month = month_index % 12 + 1
    start = date(year, month, 1)
    next_index = month_index + 1
    next_year = day.year + next_index // 12
    next_month = next_index % 12 + 1
    end = date(next_year, next_month, 1) - timedelta(days=1)
    return start, end


def _parse_date_token(token: str, today: date) -> date:
    normalized = str(token or "").strip().lower()
    if normalized in _SINGLE_DAY_TOKENS:
        return today + timedelta(days=_SINGLE_DAY_TOKENS[normalized])
    try:
        return date.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(_RANGE_HINT.format(value=token)) from exc


def _week_bounds(today: date, offset_weeks: int) -> tuple[date, date]:
    start = today - timedelta(days=today.weekday()) + timedelta(days=7 * offset_weeks)
    return start, start + timedelta(days=6)


def _parse_time_range(
    value: str, today: date | None = None
) -> tuple[datetime, datetime, str]:
    """Return ``(start_bound, end_bound, label)`` for a range expression.

    ``end_bound`` is exclusive and always the midnight after the last day, so a
    single day covers exactly 00:00-24:00 local time.
    """
    today = today or datetime.now(LOCAL_TZ).date()
    normalized = str(value or "today").strip().lower()
    compact = re.sub(r"\s+", "", normalized)

    if compact in _SINGLE_DAY_TOKENS:
        start_date = end_date = today + timedelta(days=_SINGLE_DAY_TOKENS[compact])
    elif compact in _THIS_WEEK:
        start_date, end_date = _week_bounds(today, 0)
    elif compact in _NEXT_WEEK:
        start_date, end_date = _week_bounds(today, 1)
    elif compact in _LAST_WEEK:
        start_date, end_date = _week_bounds(today, -1)
    elif compact in _THIS_MONTH:
        start_date, end_date = _month_bounds(today)
    elif compact in _NEXT_MONTH:
        start_date, end_date = _month_bounds(today, 1)
    elif compact in _LAST_MONTH:
        start_date, end_date = _month_bounds(today, -1)
    else:
        parts = [part for part in _RANGE_SPLIT_RE.split(normalized, maxsplit=1) if part]
        if len(parts) == 2:
            start_date = _parse_date_token(parts[0], today)
            end_date = _parse_date_token(parts[1], today)
        else:
            start_date = end_date = _parse_date_token(normalized, today)

    if end_date < start_date:
        raise ValueError("时间范围的结束日期不能早于开始日期。")

    start_bound = datetime.combine(start_date, time.min, tzinfo=LOCAL_TZ)
    end_bound = datetime.combine(end_date + timedelta(days=1), time.min, tzinfo=LOCAL_TZ)
    if start_date == end_date:
        label = f"{start_date:%Y-%m-%d}"
    else:
        label = f"{start_date:%Y-%m-%d}..{end_date:%Y-%m-%d}"
    return start_bound, end_bound, label