"""Extract iCalendar files from AstrBot's standard message components."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from astrbot.api.message_components import File, Plain

from .constants import MAX_ICS_BYTES


def _decode_payload(value: bytes | str) -> str | None:
    if isinstance(value, bytes):
        if len(value) > MAX_ICS_BYTES:
            return None
        try:
            text = value.decode("utf-8-sig")
        except UnicodeDecodeError:
            return None
    else:
        text = value
    text = text.strip()
    return text if "BEGIN:VCALENDAR" in text.upper() else None


async def _read_file_component(component: Any) -> tuple[str, str] | None:
    name = str(getattr(component, "name", "") or "")
    if not name.lower().endswith(".ics"):
        return None

    # File.get_file() is AstrBot's adapter-independent API. It resolves a
    # local path or downloads the component URL to a local temporary file.
    get_file = getattr(component, "get_file", None)
    if not callable(get_file):
        return None
    try:
        path = await get_file()
        if not path:
            return None
        path_obj = Path(str(path))
        if not path_obj.is_file() or path_obj.stat().st_size > MAX_ICS_BYTES:
            return None
        content = path_obj.read_bytes()
    except Exception:
        return None
    payload = _decode_payload(content)
    return (payload, name) if payload else None


async def extract_ics_from_event(event: Any) -> tuple[str, str] | None:
    """Return ``(content, filename)`` from the standard AstrBot message chain."""
    try:
        messages = event.get_messages()
    except Exception:
        return None
    for component in messages or []:
        if isinstance(component, File):
            result = await _read_file_component(component)
            if result:
                return result
        elif isinstance(component, Plain):
            payload = _decode_payload(component.text or "")
            if payload:
                return payload, "schedule.ics"
    return None
