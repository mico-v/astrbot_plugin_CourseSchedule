from __future__ import annotations

from datetime import date, datetime, timezone
from uuid import uuid4
from zoneinfo import ZoneInfo

from icalendar import Calendar, Event, vRecur

from .constants import LOCAL_TZ, MAX_EVENTS_PER_FILE


def _property_text(component: Event, name: str) -> str:
    value = component.get(name)
    return str(value) if value is not None else ""


def _property_ical(component: Event, name: str) -> str:
    value = component.get(name)
    if value is None:
        return ""
    try:
        return value.to_ical().decode("utf-8")
    except (AttributeError, UnicodeDecodeError):
        return str(value)


def _datetime_property(component: Event, name: str) -> tuple[str, str]:
    value = component.get(name)
    if value is None:
        return "", ""
    raw = _property_ical(component, name)
    params = getattr(value, "params", {})
    return raw, str(params.get("TZID") or "")


def _component_to_event(component: Event) -> dict[str, str]:
    event: dict[str, str] = {"RAW_ICAL": component.to_ical().decode("utf-8")}
    for name in ("SUMMARY", "LOCATION", "DESCRIPTION", "UID"):
        value = _property_text(component, name)
        if value:
            event[name] = value
    for name in ("DTSTART", "DTEND", "DTSTAMP"):
        value, tzid = _datetime_property(component, name)
        if value:
            event[name] = value
        if tzid:
            event[f"{name}_TZID"] = tzid
    rrule = _property_ical(component, "RRULE")
    if rrule:
        event["RRULE"] = rrule
    return event


def _parse_ics_events(content: str) -> list[dict[str, str]]:
    try:
        calendar = Calendar.from_ical(content)
    except Exception as exc:
        raise ValueError(f"不是有效的 iCalendar：{exc}") from exc

    components = list(calendar.walk("VEVENT"))
    if len(components) > MAX_EVENTS_PER_FILE:
        raise ValueError(f"VEVENT 数量超过上限 {MAX_EVENTS_PER_FILE}")
    events = [_component_to_event(component) for component in components]
    events.sort(key=lambda event: event.get("DTSTART", ""))
    return events


def _parse_ics_datetime_obj(value: str, tzid: str | None = None) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        params = f";TZID={tzid}" if tzid else ""
        parsed = Event.from_ical(
            f"BEGIN:VEVENT\r\nDTSTART{params}:{raw}\r\nEND:VEVENT\r\n"
        ).decoded("DTSTART")
    except Exception:
        return None
    if isinstance(parsed, date) and not isinstance(parsed, datetime):
        parsed = datetime.combine(parsed, datetime.min.time())
    if not isinstance(parsed, datetime):
        return None
    if parsed.tzinfo is None:
        zone = LOCAL_TZ
        if tzid:
            try:
                zone = ZoneInfo(tzid)
            except Exception:
                pass
        parsed = parsed.replace(tzinfo=zone)
    return parsed.astimezone(LOCAL_TZ)


def _format_datetime(value: str, tzid: str = "") -> str:
    parsed = _parse_ics_datetime_obj(value, tzid)
    return parsed.strftime("%Y-%m-%d %H:%M") if parsed else str(value or "")


def _format_rrule(value: str) -> str:
    if not value:
        return ""
    try:
        parts = vRecur.from_ical(value)
    except Exception:
        return value
    freq = str((parts.get("FREQ") or [""])[0])
    text = {
        "DAILY": "每天",
        "WEEKLY": "每周",
        "MONTHLY": "每月",
        "YEARLY": "每年",
    }.get(freq, freq)
    byday = parts.get("BYDAY")
    if byday:
        text += " " + ",".join(str(item) for item in byday)
    count = parts.get("COUNT")
    if count:
        text += f" 共 {count[0]} 次"
    return text.strip()


def _format_ics_schedule(events: list[dict[str, str]]) -> str:
    if not events:
        return "未解析到课程事件。"
    lines: list[str] = []
    for index, event in enumerate(events, start=1):
        start = _format_datetime(event.get("DTSTART", ""), event.get("DTSTART_TZID", ""))
        end = _format_datetime(event.get("DTEND", ""), event.get("DTEND_TZID", ""))
        line = f"{index}. {event.get('SUMMARY') or '未命名课程'}"
        if start or end:
            line += f" | {start}{' - ' if start and end else ''}{end}"
        rrule = _format_rrule(event.get("RRULE", ""))
        if rrule:
            line += f" | {rrule}"
        if event.get("LOCATION"):
            line += f" | {event['LOCATION']}"
        lines.append(line)
    return "\n".join(lines)


def _parse_schedule_ics(content: str) -> tuple[list[dict[str, str]], str]:
    events = _parse_ics_events(content)
    return events, _format_ics_schedule(events)


def _remove_property(component: Event, name: str) -> None:
    if name in component:
        del component[name]


def _python_datetime_value(value: str, tzid: str = "") -> date | datetime:
    raw = str(value or "").strip()
    if len(raw) == 8 and raw.isdigit():
        return datetime.strptime(raw, "%Y%m%d").date()
    formats = ("%Y%m%dT%H%M%SZ", "%Y%m%dT%H%M%S", "%Y%m%dT%H%M")
    for fmt in formats:
        try:
            parsed = datetime.strptime(raw, fmt)
        except ValueError:
            continue
        if raw.endswith("Z"):
            return parsed.replace(tzinfo=timezone.utc)
        if tzid:
            try:
                return parsed.replace(tzinfo=ZoneInfo(tzid))
            except Exception:
                return parsed.replace(tzinfo=LOCAL_TZ)
        return parsed
    raise ValueError(f"无法解析 iCalendar 时间：{raw}")


def _event_component(event: dict[str, str]) -> Event:
    raw = event.get("RAW_ICAL")
    if raw:
        try:
            component = Event.from_ical(raw)
        except Exception:
            component = Event()
    else:
        component = Event()

    for name in (
        "UID",
        "DTSTAMP",
        "DTSTART",
        "DTEND",
        "RRULE",
        "SUMMARY",
        "LOCATION",
        "DESCRIPTION",
    ):
        _remove_property(component, name)

    component.add("UID", event.get("UID") or f"{uuid4().hex}@astrbot-course-schedule")
    dtstamp = event.get("DTSTAMP") or datetime.now(timezone.utc).strftime(
        "%Y%m%dT%H%M%SZ"
    )
    component.add("DTSTAMP", _python_datetime_value(dtstamp))
    for name in ("DTSTART", "DTEND"):
        value = event.get(name)
        if value:
            component.add(
                name,
                _python_datetime_value(value, event.get(f"{name}_TZID", "")),
            )
    if event.get("RRULE"):
        component.add("RRULE", vRecur.from_ical(event["RRULE"]))
    for name in ("SUMMARY", "LOCATION", "DESCRIPTION"):
        if event.get(name):
            component.add(name, event[name])
    return component


def _serialize_schedule_ics(
    events: list[dict[str, str]], base_ics: str = ""
) -> str:
    if base_ics:
        try:
            calendar = Calendar.from_ical(base_ics)
            calendar.subcomponents = [
                component
                for component in calendar.subcomponents
                if component.name != "VEVENT"
            ]
        except Exception:
            calendar = Calendar()
    else:
        calendar = Calendar()
    if "PRODID" not in calendar:
        calendar.add("PRODID", "-//AstrBot CourseSchedule//CN")
    if "VERSION" not in calendar:
        calendar.add("VERSION", "2.0")
    if "CALSCALE" not in calendar:
        calendar.add("CALSCALE", "GREGORIAN")
    for event in events:
        calendar.add_component(_event_component(event))
    return calendar.to_ical().decode("utf-8")
