"""Domain operations for agent-facing course schedule tools."""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta
from typing import Any
from uuid import uuid4

from icalendar import vRecur

from .constants import LOCAL_TZ
from .occurrences import _expand_member_occurrences
from .ics import _parse_ics_datetime_obj


def normalize_datetime(value: str) -> str:
    """Normalize ISO/iCalendar input to local iCalendar date-time text."""
    raw = str(value or "").strip()
    parsed = _parse_ics_datetime_obj(raw)
    if parsed is None:
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=LOCAL_TZ)
            parsed = parsed.astimezone(LOCAL_TZ)
        except ValueError as exc:
            raise ValueError(f"无法解析时间“{raw}”，请使用 YYYY-MM-DD HH:MM 或 iCalendar 格式。") from exc
    return parsed.strftime("%Y%m%dT%H%M%S")


def make_event(
    course: str,
    start_time: str,
    end_time: str,
    *,
    location: str = "",
    description: str = "",
    rrule: str = "",
    uid: str = "",
) -> dict[str, str]:
    summary = str(course or "").strip()
    if not summary:
        raise ValueError("course 不能为空。")
    start = normalize_datetime(start_time)
    end = normalize_datetime(end_time)
    start_obj = _parse_ics_datetime_obj(start)
    end_obj = _parse_ics_datetime_obj(end)
    if not start_obj or not end_obj or end_obj <= start_obj:
        raise ValueError("end_time 必须晚于 start_time。")
    event = {
        "UID": uid or f"{uuid4().hex}@astrbot-course-schedule",
        "SUMMARY": summary,
        "DTSTART": start,
        "DTEND": end,
    }
    if location.strip():
        event["LOCATION"] = location.strip()
    if description.strip():
        event["DESCRIPTION"] = description.strip()
    if rrule.strip():
        rule_text = rrule.strip()
        try:
            parsed_rule = vRecur.from_ical(rule_text)
        except Exception as exc:
            raise ValueError(f"rrule 无法解析：{rule_text}") from exc
        if not parsed_rule.get("FREQ"):
            raise ValueError("rrule 必须包含 FREQ，例如 FREQ=WEEKLY;BYDAY=MO。")
        event["RRULE"] = rule_text
    return event


def select_member_ids(members: dict[str, Any], query: str = "") -> list[str]:
    value = str(query or "").strip()
    if not value:
        return list(members)
    tokens = [item.strip() for item in re.split(r"[,，\s]+", value) if item.strip()]
    result: list[str] = []
    for user_id, info in members.items():
        name = str(info.get("name") or "") if isinstance(info, dict) else ""
        if any(token == user_id or token in name for token in tokens):
            result.append(user_id)
    return result


def day_occurrences(members: dict[str, Any], target: date, member_ids: list[str] | None = None):
    start = datetime.combine(target, time.min, tzinfo=LOCAL_TZ)
    end = start + timedelta(days=1)
    selected = member_ids or list(members)
    rows = []
    for user_id in selected:
        info = members.get(user_id)
        if not isinstance(info, dict):
            continue
        for occurrence in _expand_member_occurrences(info, start, end):
            rows.append((user_id, info, occurrence))
    return sorted(rows, key=lambda item: (item[2]["_start"], str(item[1].get("name") or item[0])))


def merge_intervals(intervals: list[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    merged: list[tuple[datetime, datetime]] = []
    for start, end in sorted(intervals):
        if not merged or start > merged[-1][1]:
            merged.append((start, end))
        elif end > merged[-1][1]:
            merged[-1] = (merged[-1][0], end)
    return merged


def common_free_slots(
    members: dict[str, Any], target: date, member_ids: list[str],
    window_start: time, window_end: time, minimum_minutes: int,
) -> list[tuple[datetime, datetime]]:
    if not member_ids:
        return []
    day_start = datetime.combine(target, window_start, tzinfo=LOCAL_TZ)
    day_end = datetime.combine(target, window_end, tzinfo=LOCAL_TZ)
    occupied: list[tuple[datetime, datetime]] = []
    for user_id in member_ids:
        rows = day_occurrences(members, target, [user_id])
        intervals = []
        for _, _, occurrence in rows:
            start = max(day_start, occurrence["_start"])
            end = min(day_end, occurrence["_end"])
            if start < end:
                intervals.append((start, end))
        occupied.extend(merge_intervals(intervals))
    occupied = merge_intervals(occupied)
    free: list[tuple[datetime, datetime]] = []
    cursor = day_start
    for start, end in occupied:
        if cursor < start and (start - cursor).total_seconds() >= minimum_minutes * 60:
            free.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < day_end and (day_end - cursor).total_seconds() >= minimum_minutes * 60:
        free.append((cursor, day_end))
    return free
