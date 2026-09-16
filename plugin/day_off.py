"""Date argument parsing for the day-oriented commands.

Shared by 休假/调休 (which mark days) and /课表 (which views one day).  The
command tail is free text, so it is split into day tokens and everything else;
for the marking commands the remaining words become the target member query.
Only tokens that really look like dates are consumed, which keeps nicknames
intact.
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

# How far around the current year a year-less date is searched.  Four years is
# enough to reach a valid 2月29日 from any starting year.
_YEAR_SEARCH_SPAN = 4

# Relative labels for the day views, mirroring RELATIVE_DAYS.
_DAY_DELTA_TEXT = {
    0: "今天",
    1: "明天",
    2: "后天",
    3: "大后天",
    -1: "昨天",
    -2: "前天",
}

_DAY_HINT = "支持 2026-09-17、9.17、9月17日、今天、明天、后天、昨天 等写法。"

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
    "至",
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
# A punctuation range marker with spaces around it still means one range:
# "10月1日 .. 10月8日" must cover the whole span, not just its two ends.
_RANGE_JOIN_RE = re.compile(r"\s*(\.\.|~|～|—)\s*")
_TOKEN_SPLIT_RE = re.compile(r"[\s,，、;；]+")
_LEADING_PARTICLE_RE = re.compile(r"^(?:上|按|补|调|到|从|把|给|为|在|换|改)+")
_TRAILING_PARTICLE_RE = re.compile(r"(?:的(?:课|课程|课程表)?|课|课程)+$")


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _year_candidates(month: int, day: int, today: date) -> list[date]:
    """Every real date with this month/day within the search span."""
    candidates: list[date] = []
    for offset in range(-_YEAR_SEARCH_SPAN, _YEAR_SEARCH_SPAN + 1):
        candidate = _safe_date(today.year + offset, month, day)
        if candidate is not None:
            candidates.append(candidate)
    return candidates


def _pick_year(candidates: list[date], today: date, roll: str) -> date | None:
    if not candidates:
        return None
    if roll == "nearest":
        # Whichever occurrence is closest to today; a tie goes to the future.
        return min(
            candidates, key=lambda item: (abs((item - today).days), item < today)
        )
    ahead = [item for item in candidates if item >= today]
    return min(ahead) if ahead else max(candidates)


def parse_day_token(value: str, today: date, *, roll: str = "forward") -> date | None:
    """Parse one token as a day.

    Returns ``None`` when the token is not a date at all, and raises
    ``ValueError`` when it is date-shaped but not a real day (``2026-02-30``,
    ``2月30日``).

    A date without a year is resolved against ``roll``:

    ``forward`` (default)
        Always points ahead, so in December ``1月3日`` means next January.  This
        is what marking a future 休假/调休 wants.
    ``nearest``
        Whichever year puts the date closest to today, in either direction; a
        tie goes to the future.  Viewing a past day is as valid as viewing a
        coming one, so ``/课表`` uses this.
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
        parsed = _pick_year(_year_candidates(month, day, today), today, roll)
        if parsed is None:
            raise ValueError(f"日期“{raw}”不存在，请检查后重试。")
        return parsed
    return None


def _loose_day(value: str, today: date, *, roll: str = "forward") -> date | None:
    """Parse a token that may carry particles glued to the date."""
    try:
        parsed = parse_day_token(value, today, roll=roll)
    except ValueError:
        return None
    if parsed is not None:
        return parsed
    cleaned = _TRAILING_PARTICLE_RE.sub("", _LEADING_PARTICLE_RE.sub("", value.strip()))
    if not cleaned or cleaned == value.strip():
        return None
    try:
        return parse_day_token(cleaned, today, roll=roll)
    except ValueError:
        return None


def _range_days(value: str, today: date, *, roll: str = "forward") -> list[date] | None:
    parts = [part for part in _RANGE_SPLIT_RE.split(str(value or "")) if part.strip()]
    if len(parts) != 2:
        return None
    start = _loose_day(parts[0], today, roll=roll)
    end = _loose_day(parts[1], today, roll=roll)
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


def split_day_override_args(
    value: str, today: date, *, roll: str = "forward"
) -> tuple[list[date], list[str]]:
    """Split a command tail into day tokens and the remaining words.

    ``2026-10-01``, ``10月1日``, ``今天`` and ranges such as
    ``10月1日至10月8日`` all count as days; anything else is kept as a candidate
    member query.  Raises ``ValueError`` on unusable dates.
    """
    days: list[date] = []
    rest: list[str] = []
    for token in _TOKEN_SPLIT_RE.split(_RANGE_JOIN_RE.sub(r"\1", str(value or ""))):
        token = token.strip()
        if not token or token in FILLER_TOKENS:
            continue
        ranged = _range_days(token, today, roll=roll)
        if ranged:
            days.extend(ranged)
            continue
        parsed = parse_day_token(token, today, roll=roll)
        if parsed is None:
            parsed = _loose_day(token, today, roll=roll)
        if parsed is not None:
            days.append(parsed)
            continue
        rest.append(token.lstrip("@").strip())
    return days, [item for item in rest if item]


def single_day_query(value: str, today: date) -> tuple[date | None, str]:
    """Resolve the ``/课表`` argument to at most one day.

    Returns ``(day, "")`` on success, ``(None, "")`` when no argument was given
    (meaning today), and ``(None, message)`` when the argument is not a usable
    single day.  A year-less date resolves to the closest occurrence, because a
    past day is as reasonable a target as a coming one.
    """
    try:
        days, rest = split_day_override_args(value, today, roll="nearest")
    except ValueError as exc:
        return None, str(exc)
    if rest:
        return None, f"无法识别日期“{' '.join(rest)}”。{_DAY_HINT}"
    if len(days) > 1:
        return None, (
            f"一次只能查看一天（当前解析出 {len(days)} 天），"
            "例如 /课表 9.17 或 /课表 明天。"
        )
    return (days[0] if days else None), ""


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


def relative_day_text(day: date, today: date) -> str:
    """``今天``/``明天``/``昨天`` for nearby days, ``N 天后`` beyond that."""
    delta = (day - today).days
    if delta in _DAY_DELTA_TEXT:
        return _DAY_DELTA_TEXT[delta]
    return f"{delta} 天后" if delta > 0 else f"{-delta} 天前"


def day_count_text(days: list[date]) -> str:
    """``，共 8 天`` for a multi-day reply, empty for a single day."""
    count = len(set(days))
    return f"，共 {count} 天" if count > 1 else ""
