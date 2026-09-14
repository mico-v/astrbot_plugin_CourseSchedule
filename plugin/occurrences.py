from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any

from dateutil.rrule import rrulestr
from icalendar import Event

from .constants import DAY_OVERRIDE_HOLIDAY, DAY_OVERRIDE_SHIFT, LOCAL_TZ
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


def _parse_day_text(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value or "").strip())
    except ValueError:
        return None


def _member_day_overrides(member_info: dict[str, Any]) -> dict[str, dict[str, str]]:
    """Return ``{"YYYY-MM-DD": {"kind", "source_day"}}`` for one member.

    The store merges the scope-wide (all-members) markers with the member's own
    markers before attaching them, so a member entry always wins over the
    scope entry for the same day.
    """
    raw = member_info.get("_day_overrides") if isinstance(member_info, dict) else None
    if not isinstance(raw, dict):
        return {}
    overrides: dict[str, dict[str, str]] = {}
    for day, rule in raw.items():
        if not isinstance(rule, dict):
            continue
        parsed_day = _parse_day_text(day)
        if parsed_day is None:
            continue
        kind = str(rule.get("kind") or "").strip().lower()
        if kind == DAY_OVERRIDE_HOLIDAY:
            overrides[parsed_day.isoformat()] = {"kind": kind, "source_day": ""}
        elif kind == DAY_OVERRIDE_SHIFT:
            source = _parse_day_text(rule.get("source_day"))
            if source is not None:
                overrides[parsed_day.isoformat()] = {
                    "kind": kind,
                    "source_day": source.isoformat(),
                }
    return overrides


def _expand_events_on_day(
    events: list[dict[str, Any]], day: date
) -> list[tuple[int, dict[str, Any]]]:
    """Expand every event over one calendar day, keeping the source event index."""
    day_start = datetime.combine(day, time.min, tzinfo=LOCAL_TZ)
    day_end = day_start + timedelta(days=1)
    found: list[tuple[int, dict[str, Any]]] = []
    for index, event in enumerate(events, start=1):
        for occurrence in _expand_event_occurrences(event, day_start, day_end):
            if occurrence["_start"].date() == day:
                found.append((index, occurrence))
    return found


def _expand_indexed_occurrences(
    events: list[dict[str, Any]],
    overrides: dict[str, dict[str, str]],
    start_bound: datetime,
    end_bound: datetime,
) -> list[tuple[int, dict[str, Any]]]:
    """Expand events for a window, applying 休假/调休 markers.

    A day marked 休假 loses every occurrence that starts on it.  A day marked
    调休 loses its own occurrences and shows the source day's courses instead,
    shifted to the same clock times.  The source day is read from the stored
    events directly, so marking the source day 休假 as well (the usual holiday
    announcement) does not empty the make-up day.
    """
    indexed: list[tuple[int, dict[str, Any]]] = []
    for index, event in enumerate(events, start=1):
        for occurrence in _expand_event_occurrences(event, start_bound, end_bound):
            if occurrence["_start"].date().isoformat() in overrides:
                continue
            indexed.append((index, occurrence))

    source_cache: dict[str, list[tuple[int, dict[str, Any]]]] = {}
    for day_text, rule in overrides.items():
        if rule["kind"] != DAY_OVERRIDE_SHIFT:
            continue
        target_day = _parse_day_text(day_text)
        source_day = _parse_day_text(rule["source_day"])
        if target_day is None or source_day is None:
            continue
        target_start = datetime.combine(target_day, time.min, tzinfo=LOCAL_TZ)
        target_end = target_start + timedelta(days=1)
        if target_start >= end_bound or target_end <= start_bound:
            continue
        source_start = datetime.combine(source_day, time.min, tzinfo=LOCAL_TZ)
        cached = source_cache.get(rule["source_day"])
        if cached is None:
            cached = _expand_events_on_day(events, source_day)
            source_cache[rule["source_day"]] = cached
        for index, occurrence in cached:
            start = target_start + (occurrence["_start"] - source_start)
            shifted = dict(occurrence)
            shifted["_start"] = start
            shifted["_end"] = start + (occurrence["_end"] - occurrence["_start"])
            shifted["_shifted_from"] = source_day.isoformat()
            indexed.append((index, shifted))

    indexed.sort(key=lambda item: item[1]["_start"])
    return indexed


def _expand_member_occurrences(
    member_info: dict[str, Any], start_bound: datetime, end_bound: datetime
) -> list[dict[str, Any]]:
    events = member_info.get("events")
    if not isinstance(events, list):
        return []

    indexed = _expand_indexed_occurrences(
        [event for event in events if isinstance(event, dict)],
        _member_day_overrides(member_info),
        start_bound,
        end_bound,
    )
    return [occurrence for _index, occurrence in indexed]


def _day_bounds(target_date: date) -> tuple[datetime, datetime]:
    start = datetime.combine(target_date, time.min, tzinfo=LOCAL_TZ)
    return start, start + timedelta(days=1)


def _duration_hours(occurrences: list[dict[str, Any]]) -> float:
    seconds = sum((item["_end"] - item["_start"]).total_seconds() for item in occurrences)
    return seconds / 3600
