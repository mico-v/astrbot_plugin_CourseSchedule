from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from .constants import LOCAL_TZ, MAX_EVENTS_PER_FILE


def _unfold_ics_lines(content: str) -> list[str]:
    lines: list[str] = []
    for raw_line in content.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw_line.startswith((" ", "\t")) and lines:
            lines[-1] += raw_line[1:]
        else:
            lines.append(raw_line)

    return lines


def _decode_ics_text(value: str) -> str:
    return (
        value.replace("\\n", "\n")
        .replace("\\N", "\n")
        .replace("\\,", ",")
        .replace("\\;", ";")
        .replace("\\\\", "\\")
        .strip()
    )


def _parse_ics_datetime(value: str) -> str:
    raw = value.strip()
    if not raw:
        return ""

    formats = [
        ("%Y%m%dT%H%M%SZ", "%Y-%m-%d %H:%M UTC"),
        ("%Y%m%dT%H%M%S", "%Y-%m-%d %H:%M"),
        ("%Y%m%dT%H%M", "%Y-%m-%d %H:%M"),
        ("%Y%m%d", "%Y-%m-%d"),
    ]
    for input_format, output_format in formats:
        try:
            return datetime.strptime(raw, input_format).strftime(output_format)
        except ValueError:
            continue

    return raw


def _parse_ics_datetime_obj(value: str, tzid: str | None = None) -> datetime | None:
    raw = value.strip()
    if not raw:
        return None

    tz = LOCAL_TZ
    if tzid:
        try:
            tz = ZoneInfo(tzid)
        except Exception:
            tz = LOCAL_TZ

    formats = [
        ("%Y%m%dT%H%M%SZ", timezone.utc),
        ("%Y%m%dT%H%M%S", tz),
        ("%Y%m%dT%H%M", tz),
        ("%Y%m%d", tz),
    ]
    for input_format, timezone_info in formats:
        try:
            parsed = datetime.strptime(raw, input_format)
            return parsed.replace(tzinfo=timezone_info).astimezone(LOCAL_TZ)
        except ValueError:
            continue

    return None


def _parse_rrule(value: str) -> str:
    if not value:
        return ""

    parts: dict[str, str] = {}
    for item in value.split(";"):
        key, _, item_value = item.partition("=")
        if key and item_value:
            parts[key.upper()] = item_value

    freq_map = {
        "DAILY": "每天",
        "WEEKLY": "每周",
        "MONTHLY": "每月",
        "YEARLY": "每年",
    }
    text = freq_map.get(parts.get("FREQ", ""), parts.get("FREQ", ""))
    if parts.get("BYDAY"):
        text += f" {parts['BYDAY']}"
    if parts.get("COUNT"):
        text += f" 共 {parts['COUNT']} 次"
    if parts.get("UNTIL"):
        text += f" 至 {_parse_ics_datetime(parts['UNTIL'])}"

    return text.strip()


def _parse_rrule_parts(value: str) -> dict[str, str]:
    parts: dict[str, str] = {}
    for item in value.split(";"):
        key, _, item_value = item.partition("=")
        if key and item_value:
            parts[key.upper()] = item_value
    return parts


def _parse_ics_key(key: str) -> tuple[str, dict[str, str]]:
    parts = key.split(";")
    name = parts[0].upper()
    params: dict[str, str] = {}
    for item in parts[1:]:
        param_name, _, param_value = item.partition("=")
        if param_name and param_value:
            params[param_name.upper()] = param_value
    return name, params


def _parse_ics_events(content: str) -> list[dict[str, str]]:
    events: list[dict[str, str]] = []
    current: dict[str, str] | None = None

    for line in _unfold_ics_lines(content):
        if not line:
            continue

        upper_line = line.upper()
        if upper_line == "BEGIN:VEVENT":
            current = {}
            continue

        if upper_line == "END:VEVENT":
            if current:
                events.append(current)
            current = None
            continue

        if current is None or ":" not in line:
            continue

        key, value = line.split(":", 1)
        name, params = _parse_ics_key(key)
        if name in {"SUMMARY", "LOCATION", "DESCRIPTION"}:
            current[name] = _decode_ics_text(value)
        elif name in {"DTSTART", "DTEND", "RRULE"}:
            current[name] = value.strip()
            if params.get("TZID"):
                current[f"{name}_TZID"] = params["TZID"]

    events.sort(key=lambda event: event.get("DTSTART", ""))
    return events[:MAX_EVENTS_PER_FILE]


def _format_ics_schedule(events: list[dict[str, str]]) -> str:
    if not events:
        return "未解析到课程事件。"

    lines: list[str] = []
    for index, event in enumerate(events, start=1):
        summary = event.get("SUMMARY") or "未命名课程"
        start = _parse_ics_datetime(event.get("DTSTART", ""))
        end = _parse_ics_datetime(event.get("DTEND", ""))
        location = event.get("LOCATION")
        rrule = _parse_rrule(event.get("RRULE", ""))

        time_text = start
        if end:
            time_text = f"{start} - {end}" if start else end

        line = f"{index}. {summary}"
        if time_text:
            line += f" | {time_text}"
        if rrule:
            line += f" | {rrule}"
        if location:
            line += f" | {location}"
        lines.append(line)

    return "\n".join(lines)


def _parse_schedule_ics(content: str) -> tuple[list[dict[str, str]], str]:
    events = _parse_ics_events(content)
    return events, _format_ics_schedule(events)
