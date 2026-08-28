from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any

from dateutil.rrule import rrulestr
from icalendar import Event

from .constants import LOCAL_TZ
from .ics import _parse_ics_datetime_obj


def _event_datetimes(event: dict[str, str]) -> tuple[datetime | None, datetime | None]:
    start = _parse_ics_datetime_obj(event.get("DTSTART", ""), event.get("DTSTART_TZID"))
    end = _parse_ics_datetime_obj(event.get("DTEND", ""), event.get("DTEND_TZID"))
    if start and not end:
        duration = None
        if event.get("RAW_ICAL"):
            try:
                duration = Event.from_ical(event["RAW_ICAL"]).decoded("DURATION")
            except Exception:
                pass
        if not isinstance(duration, timedelta):
            duration = (
                timedelta(days=1)
                if len(event.get("DTSTART", "")) == 8
                else timedelta(hours=1, minutes=30)
            )
        end = start + duration
    if start and end and end <= start:
        end = start + timedelta(hours=1, minutes=30)
    return start, end


def _copy_occurrence(event: dict[str, str], start: datetime, end: datetime) -> dict[str, Any]:
    copied: dict[str, Any] = dict(event)
    copied["_start"] = start
    copied["_end"] = end
    return copied


def _local_datetime(value: Any) -> datetime | None:
    if isinstance(value, date) and not isinstance(value, datetime):
        value = datetime.combine(value, time.min)
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=LOCAL_TZ)
    return value.astimezone(LOCAL_TZ)


def _recurrence_dates(event: dict[str, str], name: str) -> list[datetime]:
    raw = event.get("RAW_ICAL")
    if not raw:
        return []
    try:
        component = Event.from_ical(raw)
    except Exception:
        return []
    properties = component.get(name)
    if properties is None:
        return []
    if not isinstance(properties, list):
        properties = [properties]
    dates: list[datetime] = []
    for prop in properties:
        for item in getattr(prop, "dts", [prop]):
            parsed = _local_datetime(getattr(item, "dt", item))
            if parsed:
                dates.append(parsed)
    return dates


def _expand_event_occurrences(
    event: dict[str, str], start_bound: datetime, end_bound: datetime
) -> list[dict[str, Any]]:
    start, end = _event_datetimes(event)
    if not start or not end:
        return []

    duration = end - start
    rrule = event.get("RRULE", "")
    if rrule:
        try:
            rule = rrulestr(rrule, dtstart=start)
            occurrence_starts = list(
                rule.between(start_bound - duration, end_bound, inc=True)
            )
        except (TypeError, ValueError, OverflowError):
            occurrence_starts = []
    else:
        occurrence_starts = [start]

    occurrence_starts.extend(_recurrence_dates(event, "RDATE"))
    excluded = set(_recurrence_dates(event, "EXDATE"))

    occurrences: list[dict[str, Any]] = []
    for occurrence_start in occurrence_starts:
        if occurrence_start.tzinfo is None:
            occurrence_start = occurrence_start.replace(tzinfo=LOCAL_TZ)
        occurrence_start = occurrence_start.astimezone(LOCAL_TZ)
        if occurrence_start in excluded:
            continue
        occurrence_end = occurrence_start + duration
        if occurrence_start < end_bound and occurrence_end > start_bound:
            occurrences.append(_copy_occurrence(event, occurrence_start, occurrence_end))

    occurrences.sort(key=lambda item: item["_start"])
    deduplicated: list[dict[str, Any]] = []
    seen: set[datetime] = set()
    for occurrence in occurrences:
        if occurrence["_start"] not in seen:
            seen.add(occurrence["_start"])
            deduplicated.append(occurrence)
    return deduplicated


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


def _duration_hours(occurrences: list[dict[str, Any]]) -> float:
    seconds = sum((item["_end"] - item["_start"]).total_seconds() for item in occurrences)
    return seconds / 3600
