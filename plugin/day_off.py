"""Argument parsing for the 休假 (day off) and 调休 (make-up day) commands.

The command tail is free text, so it is split into day tokens and everything
else; the remaining words become the target member query.  Only tokens that
really look like dates are consumed, which keeps nicknames intact.
"""

from __future__ import annotations

import re
from datetime import date, timedelta

from .constants import MAX_DAY_OVERRIDE_RANGE_DAYS

RELATIVE_DAYS = {
    "今天": 0,
    "今日": 0,
    "today": 0,
    "now": 0,
    "明天": 1,
    "明日": 1,
    "tomorrow": 1,
    "后天": 2,
    "後天": 2,
    "大后天": 3,
    "昨天": -1,
    "昨日": -1,
    "yesterday": -1,
    "前天": -2,
}

# Words that only glue the sentence together ("10 月 11 日 上 10 月 8 日 的课").
FILLER_TOKENS = {
    "上",
    "按",
    "补",
    "调",
    "换",
    "改成",
    "改到",
    "换成",
    "替换",
    "标记",
    "设成",
    "设为",
    "假期",
    "放假",
    "休假",
    "调休",
    "的",
    "的课",
    "课",
    "课程",
    "到",
    "从",
    "把",
    "给",
    "为",
    "在",
    "和",
    "与",
    "→",
    "->",
    "=>",
    "=",
    "--",
}

_DATE_TEXT_RE = re.compile(r"^(\d{4})[-/.年](\d{1,2})[-/.月](\d{1,2})日?$")
_MONTH_DAY_RE = re.compile(r"^(\d{1,2})[-/.月](\d{1,2})日?$")
_RANGE_SPLIT_RE = re.compile(r"\s*(?:\.\.|~|～|—|至|到)\s*")
_TOKEN_SPLIT_RE = re.compile(r"[\s,，、;；]+")
_LEADING_PARTICLE_RE = re.compile(r"^(?:上|按|补|调|到|从|把|给|为|在|换|改)+")
_TRAILING_PARTICLE_RE = re.compile(r"(?:的(?:课|课程|课程表)?|课|课程)+$")


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def parse_day_token(value: str, today: date) -> date | None:
    """Parse one token as a day.

    Returns ``None`` when the token is not a date at all, and raises
    ``ValueError`` when it is date-shaped but not a real day (``2026-02-30``).
    A date without a year always points forward: in December ``1月3日`` means
    next January.
    """
    raw = str(value or "").strip()
    if not raw:
        return None
    relative = RELATIVE_DAYS.get(raw.lower())
    if relative is not None:
        return today + timedelta(days=relative)

    matched = _DATE_TEXT_RE.match(raw)
    if matched:
        year, month, day = (int(part) for part in matched.groups())
        parsed = _safe_date(year, month, day)
        if parsed is None:
            raise ValueError(f"日期“{raw}”不存在，请检查后重试。")
        return parsed

    matched = _MONTH_DAY_RE.match(raw)
    if matched:
        month, day = (int(part) for part in matched.groups())
        parsed = _safe_date(today.year, month, day)
        if parsed is None:
            raise ValueError(f"日期“{raw}”不存在，请检查后重试。")
        if parsed < today:
            parsed = _safe_date(today.year + 1, month, day)
        return parsed
    return None


def _loose_day(value: str, today: date) -> date | None:
    """Parse a token that may carry particles glued to the date."""
    try:
        parsed = parse_day_token(value, today)
    except ValueError:
        return None
    if parsed is not None:
        return parsed
    cleaned = _TRAILING_PARTICLE_RE.sub("", _LEADING_PARTICLE_RE.sub("", value.strip()))
    if not cleaned or cleaned == value.strip():
        return None
    try:
        return parse_day_token(cleaned, today)
    except ValueError:
        return None


def _range_days(value: str, today: date) -> list[date] | None:
    parts = [part for part in _RANGE_SPLIT_RE.split(str(value or "")) if part.strip()]
    if len(parts) != 2:
        return None
    start = _loose_day(parts[0], today)
    end = _loose_day(parts[1], today)
    if start is None or end is None:
        return None
    if end < start:
        raise ValueError("日期范围的结束日期不能早于开始日期。")
    span = (end - start).days + 1
    if span > MAX_DAY_OVERRIDE_RANGE_DAYS:
        raise ValueError(
            f"一次最多标记 {MAX_DAY_OVERRIDE_RANGE_DAYS} 天，请拆分成多个范围后重试。"
        )
    return [start + timedelta(days=offset) for offset in range(span)]


def split_day_override_args(value: str, today: date) -> tuple[list[date], list[str]]:
    """Split a command tail into marked days and the remaining target words.

    ``2026-10-01``, ``10月1日``, ``今天`` and ranges such as
    ``10月1日至10月8日`` all count as days; anything else is kept as a
    candidate member query.  Raises ``ValueError`` on unusable dates.
    """
    days: list[date] = []
    rest: list[str] = []
    for token in _TOKEN_SPLIT_RE.split(str(value or "")):
        token = token.strip()
        if not token or token in FILLER_TOKENS:
            continue
        ranged = _range_days(token, today)
        if ranged:
            days.extend(ranged)
            continue
        parsed = parse_day_token(token, today)
        if parsed is None:
            parsed = _loose_day(token, today)
        if parsed is not None:
            days.append(parsed)
            continue
        rest.append(token.lstrip("@").strip())
    return days, [item for item in rest if item]


def format_day_list(days: list[date]) -> str:
    """Compact label for command replies: one day, a range, or a short list."""
    ordered = sorted(set(days))
    if not ordered:
        return ""
    if len(ordered) == 1:
        return f"{ordered[0]:%Y-%m-%d}"
    first, last = ordered[0], ordered[-1]
    if (last - first).days + 1 == len(ordered):
        return f"{first:%Y-%m-%d} 至 {last:%Y-%m-%d}"
    shown = "、".join(f"{item:%Y-%m-%d}" for item in ordered[:6])
    if len(ordered) > 6:
        shown += " 等"
    return shown


def day_count_text(days: list[date]) -> str:
    """``，共 8 天`` for a multi-day reply, empty for a single day."""
    count = len(set(days))
    return f"，共 {count} 天" if count > 1 else ""
