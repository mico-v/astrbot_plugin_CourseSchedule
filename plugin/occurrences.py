from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any

from .constants import LOCAL_TZ, WEEKDAY_CODES
from .ics import _parse_ics_datetime_obj, _parse_rrule_parts


def _event_datetimes(event: dict[str, str]) -> tuple[datetime | None, datetime | None]:
    start = _parse_ics_datetime_obj(event.get("DTSTART", ""), event.get("DTSTART_TZID"))
    end = _parse_ics_datetime_obj(event.get("DTEND", ""), event.get("DTEND_TZID"))
    if start and not end:
        end = start + timedelta(hours=1, minutes=30)
    if start and end and end <= start:
        end = start + timedelta(hours=1, minutes=30)
    return start, end


def _copy_occurrence(event: dict[str, str], start: datetime, end: datetime) -> dict[str, Any]:
    copied: dict[str, Any] = dict(event)
    copied["_start"] = start
    copied["_end"] = end
    return copied


def _expand_event_occurrences(
    event: dict[str, str], start_bound: datetime, end_bound: datetime
) -> list[dict[str, Any]]:
    start, end = _event_datetimes(event)
    if not start or not end:
        return []

    duration = end - start
    rrule = event.get("RRULE", "")
    if not rrule:
        if start < end_bound and end > start_bound:
            return [_copy_occurrence(event, start, end)]
        return []

    parts = _parse_rrule_parts(rrule)
    if parts.get("FREQ") != "WEEKLY":
        if start < end_bound and end > start_bound:
            return [_copy_occurrence(event, start, end)]
        return []

    weekdays = [start.weekday()]
    if parts.get("BYDAY"):
        weekdays = [
            WEEKDAY_CODES.index(code)
            for code in parts["BYDAY"].split(",")
            if code in WEEKDAY_CODES
        ] or weekdays

    until = _parse_ics_datetime_obj(parts.get("UNTIL", ""), event.get("DTSTART_TZID"))
    count = int(parts.get("COUNT") or 0)
    interval = max(int(parts.get("INTERVAL") or 1), 1)
    occurrences: list[dict[str, Any]] = []
    generated = 0

    cursor_date = start_bound.date() - timedelta(days=7 * interval)
    last_date = end_bound.date() + timedelta(days=7)
    while cursor_date <= last_date:
        if cursor_date.weekday() in weekdays:
            occurrence_start = datetime.combine(
                cursor_date, start.timetz().replace(tzinfo=None), tzinfo=LOCAL_TZ
            )
            weeks_from_start = (occurrence_start.date() - start.date()).days // 7
            if occurrence_start >= start and weeks_from_start % interval == 0:
                generated += 1
                occurrence_end = occurrence_start + duration
                if count and generated > count:
                    break
                if until and occurrence_start > until:
                    break
                if occurrence_start < end_bound and occurrence_end > start_bound:
                    occurrences.append(_copy_occurrence(event, occurrence_start, occurrence_end))
        cursor_date += timedelta(days=1)

    occurrences.sort(key=lambda item: item["_start"])
    return occurrences


def _expand_member_occurrences(
    member_info: dict[str, Any], start_bound: datetime, end_bound: datetime
) -> list[dict[str, Any]]:
    events = member_info.get("events")
    if not isinstance(events, list):
        return []

    occurrences: list[dict[str, Any]] = []
    for event in events:
        if isinstance(event, dict):
            occurrences.extend(_expand_event_occurrences(event, start_bound, end_bound))

    occurrences.sort(key=lambda item: item["_start"])
    return occurrences


def _day_bounds(target_date: date) -> tuple[datetime, datetime]:
    start = datetime.combine(target_date, time.min, tzinfo=LOCAL_TZ)
    return start, start + timedelta(days=1)


def _week_bounds(today: date) -> tuple[datetime, datetime]:
    start_date = today - timedelta(days=today.weekday())
    start = datetime.combine(start_date, time.min, tzinfo=LOCAL_TZ)
    return start, start + timedelta(days=7)


def _format_occurrence_line(occurrence: dict[str, Any]) -> str:
    start: datetime = occurrence["_start"]
    end: datetime = occurrence["_end"]
    summary = occurrence.get("SUMMARY") or "未命名课程"
    location = occurrence.get("LOCATION")
    text = f"{start:%H:%M}-{end:%H:%M} {summary}"
    if location:
        text += f" @ {location}"
    return text


def _current_or_next(occurrences: list[dict[str, Any]], now: datetime) -> tuple[str, dict[str, Any] | None]:
    for occurrence in occurrences:
        if occurrence["_start"] <= now < occurrence["_end"]:
            return "正在上", occurrence
        if occurrence["_start"] > now:
            return "下一节", occurrence
    return "无课", None


def _duration_hours(occurrences: list[dict[str, Any]]) -> float:
    seconds = sum((item["_end"] - item["_start"]).total_seconds() for item in occurrences)
    return seconds / 3600
