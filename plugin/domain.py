"""Domain operations for agent-facing course schedule tools."""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta
from html import unescape
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


def _format_duration_minutes(minutes: int) -> str:
    minutes = max(0, int(minutes))
    hours, remainder = divmod(minutes, 60)
    if hours and remainder:
        return f"{hours}小时{remainder}分钟"
    if hours:
        return f"{hours}小时"
    return f"{minutes}分钟"


def _format_remaining(delta: timedelta) -> str:
    seconds = max(0, int(delta.total_seconds()))
    minutes = (seconds + 59) // 60
    if minutes < 1:
        return "不到1分钟"
    days, minutes = divmod(minutes, 24 * 60)
    if days:
        hours, remainder = divmod(minutes, 60)
        parts = [f"{days}天"]
        if hours:
            parts.append(f"{hours}小时")
        if remainder:
            parts.append(f"{remainder}分钟")
        return "".join(parts)
    return _format_duration_minutes(minutes)


def _daily_row_course(occurrence: dict[str, Any] | None) -> tuple[str, str]:
    if not occurrence:
        return "暂无课程安排", ""
    course = str(occurrence.get("SUMMARY") or "未命名课程")
    location = str(occurrence.get("LOCATION") or "").strip()
    return course, location


def _display_name(value: Any) -> str:
    """Normalize names returned as HTML entities or UTF-16 surrogate pairs."""
    name = unescape(str(value or ""))
    try:
        return name.encode("utf-16", "surrogatepass").decode("utf-16")
    except UnicodeError:
        return name


def daily_member_rows(
    members: dict[str, Any],
    target: date,
    *,
    now: datetime | None = None,
    member_ids: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Build one status row for each member on a selected calendar day.

    For today, rows are ordered as active, upcoming, finished, and no class.
    Future dates use a countdown to their first class; past dates are shown
    as completed static plans because a live countdown would be misleading.
    """
    current = now or datetime.now(LOCAL_TZ)
    if current.tzinfo is None:
        current = current.replace(tzinfo=LOCAL_TZ)
    else:
        current = current.astimezone(LOCAL_TZ)
    selected = list(members) if member_ids is None else list(member_ids)
    is_today = target == current.date()
    start_bound = datetime.combine(target, time.min, tzinfo=LOCAL_TZ)
    end_bound = start_bound + timedelta(days=1)
    result: list[dict[str, Any]] = []

    for user_id in selected:
        info = members.get(user_id)
        if not isinstance(info, dict):
            continue
        occurrences = _expand_member_occurrences(info, start_bound, end_bound)
        occurrences.sort(key=lambda item: item["_start"])
        active = next(
            (
                item
                for item in occurrences
                if is_today and item["_start"] <= current < item["_end"]
            ),
            None,
        )
        upcoming = next(
            (
                item
                for item in occurrences
                if (is_today and item["_start"] > current)
                or (target > current.date())
            ),
            None,
        )

        if active:
            state = "active"
            featured = active
            status = "正在上课"
            countdown_label = "距下课"
            countdown = _format_remaining(active["_end"] - current)
            progress = min(
                1.0,
                max(
                    0.0,
                    (current - active["_start"]).total_seconds()
                    / max(1.0, (active["_end"] - active["_start"]).total_seconds()),
                ),
            )
            sort_time = active["_start"].timestamp()
            sort_priority = 0
        elif upcoming:
            state = "upcoming"
            featured = upcoming
            status = "下一节即将上课"
            countdown_label = "距上课"
            countdown = _format_remaining(upcoming["_start"] - current)
            progress = 0.0
            sort_time = upcoming["_start"].timestamp()
            sort_priority = 1
        elif occurrences and target <= current.date():
            state = "finished"
            featured = occurrences[-1]
            status = "今日课程已结束" if is_today else "当天课程已结束"
            countdown_label = "课程状态"
            countdown = "今天的课程都上完啦" if is_today else "这一天的课程都上完啦"
            progress = 1.0
            sort_time = -occurrences[-1]["_end"].timestamp()
            sort_priority = 2
        elif occurrences:
            state = "upcoming"
            featured = occurrences[0]
            status = "下一节即将上课"
            countdown_label = "距上课"
            countdown = _format_remaining(occurrences[0]["_start"] - current)
            progress = 0.0
            sort_time = occurrences[0]["_start"].timestamp()
            sort_priority = 1
        else:
            state = "none"
            featured = None
            status = "今日无课" if is_today else "当天无课"
            countdown_label = "课程状态"
            countdown = "休息日，安排得明明白白"
            progress = 0.0
            sort_time = float("inf")
            sort_priority = 3

        course, location = _daily_row_course(featured)
        if featured:
            duration_minutes = max(
                1, round((featured["_end"] - featured["_start"]).total_seconds() / 60)
            )
            duration = _format_duration_minutes(duration_minutes)
            time_text = f"{featured['_start']:%H:%M} - {featured['_end']:%H:%M}"
        else:
            duration_minutes = 0
            duration = "—"
            time_text = "今天没有安排课程"

        result.append(
            {
                "user_id": str(user_id),
                "name": _display_name(info.get("name") or user_id),
                "status": status,
                "status_key": state,
                "course": course,
                "location": location,
                "time": time_text,
                "duration": duration,
                "duration_minutes": duration_minutes,
                "countdown_label": countdown_label,
                "countdown": countdown,
                "progress": progress,
                "course_count": len(occurrences),
                "sort_priority": sort_priority,
                "sort_time": sort_time,
            }
        )

    return sorted(
        result,
        key=lambda row: (
            row["sort_priority"],
            row["sort_time"],
            str(row["name"]).casefold(),
            row["user_id"],
        ),
    )


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
