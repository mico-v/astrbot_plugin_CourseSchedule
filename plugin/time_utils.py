from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from .constants import LOCAL_TZ


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _datetime_to_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def _parse_timestamp(value: Any) -> datetime | None:
    if value in (None, ""):
        return None

    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=LOCAL_TZ).astimezone(timezone.utc)
        return value.astimezone(timezone.utc)

    if isinstance(value, (int, float)):
        timestamp = float(value)
        if timestamp > 10_000_000_000:
            timestamp /= 1000
        try:
            return datetime.fromtimestamp(timestamp, timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None

    text = str(value).strip()
    if not text:
        return None

    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        try:
            return _parse_timestamp(float(text))
        except ValueError:
            return None

    normalized = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=LOCAL_TZ)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        pass

    for input_format in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            parsed = datetime.strptime(text, input_format)
            return parsed.replace(tzinfo=LOCAL_TZ).astimezone(timezone.utc)
        except ValueError:
            continue

    return None


def _timestamp_iso(value: Any | None = None) -> str:
    parsed = _parse_timestamp(value) if value is not None else None
    return _datetime_to_iso(parsed or datetime.now(timezone.utc))
