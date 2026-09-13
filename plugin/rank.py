"""Class-hours aggregation behind the group ranking board."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Any

from .constants import LOCAL_TZ
from .domain import _display_name, _format_duration_minutes, merge_intervals
from .occurrences import _expand_member_occurrences

DEFAULT_RANK_PERIOD = "本周"
DEFAULT_RANK_TOP_N = 20
DEFAULT_RANK_METRIC = "union"
RANK_MAX_RANGE_DAYS = 366

_SUM_METRIC_ALIASES = {"sum", "total", "累加", "总计", "合计"}


def _normalize_metric(value: str) -> str:
    """Return the canonical metric name; anything unknown falls back to union."""
    return "sum" if str(value or "").strip().lower() in _SUM_METRIC_ALIASES else DEFAULT_RANK_METRIC


def _is_all_day(event: dict[str, Any]) -> bool:
    """DATE-only VEVENTs decode to a full-day span, which would dwarf real classes."""
    raw = str(event.get("DTSTART") or "").strip()
    return len(raw) == 8 and raw.isdigit()


def _matches_keywords(event: dict[str, Any], keywords: Sequence[str]) -> bool:
    if not keywords:
        return False
    summary = _display_name(event.get("SUMMARY") or "").casefold()
    return any(keyword.casefold() in summary for keyword in keywords if keyword)


def clipped_occurrences(
    occurrences: Iterable[dict[str, Any]],
    start_bound: datetime,
    end_bound: datetime,
    *,
    include_all_day: bool = False,
    exclude_keywords: Sequence[str] = (),
) -> list[dict[str, Any]]:
    """Clip occurrences to the window and drop the ones the metric must not count.

    ``_expand_event_occurrences`` only requires an occurrence to *overlap* the
    window, so a class running 23:00-01:00 is returned in full for a single-day
    window.  Every occurrence is trimmed to the window here before it can
    contribute to a duration.
    """
    clipped: list[dict[str, Any]] = []
    for occurrence in occurrences:
        if not include_all_day and _is_all_day(occurrence):
            continue
        if _matches_keywords(occurrence, exclude_keywords):
            continue
        start = max(occurrence["_start"], start_bound)
        end = min(occurrence["_end"], end_bound)
        if end <= start:
            continue
        item = dict(occurrence)
        item["_start"] = start
        item["_end"] = end
        clipped.append(item)
    return clipped


def _total_minutes(
    intervals: list[tuple[datetime, datetime]], *, merge: bool
) -> int:
    segments = merge_intervals(intervals) if merge else intervals
    seconds = sum((end - start).total_seconds() for start, end in segments)
    return round(seconds / 60)


def build_rank_rows(
    members: dict[str, Any],
    start_bound: datetime,
    end_bound: datetime,
    *,
    now: datetime | None = None,
    metric: str = DEFAULT_RANK_METRIC,
    include_empty: bool = False,
    include_all_day: bool = False,
    exclude_keywords: Sequence[str] = (),
) -> list[dict[str, Any]]:
    """Rank members by how much class time their schedule occupies in a window.

    ``minutes`` counts the whole window, including classes that have not happened
    yet, so the board stays stable while a week is in progress; ``elapsed_minutes``
    covers the part that already finished.  With the default ``union`` metric two
    courses sharing a time slot count once, which is what the member's occupied
    time actually is.  Rows with no counted course get ``rank`` 0 and are only
    returned when ``include_empty`` is set.
    """
    current = now or datetime.now(LOCAL_TZ)
    if current.tzinfo is None:
        current = current.replace(tzinfo=LOCAL_TZ)
    else:
        current = current.astimezone(LOCAL_TZ)
    merge = _normalize_metric(metric) == "union"

    rows: list[dict[str, Any]] = []
    for user_id, info in members.items():
        if not isinstance(info, dict):
            continue
        raw_occurrences = _expand_member_occurrences(info, start_bound, end_bound)
        counted = clipped_occurrences(
            raw_occurrences,
            start_bound,
            end_bound,
            include_all_day=include_all_day,
            exclude_keywords=exclude_keywords,
        )
        if not counted and not include_empty:
            continue
        intervals = [(item["_start"], item["_end"]) for item in counted]
        finished = [(start, end) for start, end in intervals if end <= current]
        minutes = _total_minutes(intervals, merge=merge)
        elapsed_minutes = _total_minutes(finished, merge=merge)
        rows.append(
            {
                "rank": 0,
                "user_id": str(user_id),
                "name": _display_name(info.get("name") or user_id),
                "minutes": minutes,
                "hours_text": _format_duration_minutes(minutes),
                "elapsed_minutes": elapsed_minutes,
                "elapsed_text": _format_duration_minutes(elapsed_minutes),
                "course_count": len(intervals),
                "course_names": len(
                    {
                        _display_name(item.get("SUMMARY") or "").strip()
                        or "未命名课程"
                        for item in counted
                    }
                ),
                "all_day_count": sum(
                    1 for item in raw_occurrences if _is_all_day(item)
                ),
                "progress": 0.0,
            }
        )

    rows.sort(
        key=lambda row: (
            -row["minutes"],
            -row["course_count"],
            str(row["name"]).casefold(),
            row["user_id"],
        )
    )

    leader = rows[0]["minutes"] if rows else 0
    shared_rank = 0
    previous_minutes: int | None = None
    for index, row in enumerate(rows, start=1):
        if row["minutes"] <= 0:
            # Nobody without counted course time holds a position on the board.
            continue
        if previous_minutes != row["minutes"]:
            shared_rank = index
            previous_minutes = row["minutes"]
        row["rank"] = shared_rank
        row["progress"] = row["minutes"] / leader if leader else 0.0
    return rows
