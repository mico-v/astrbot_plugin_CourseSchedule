from __future__ import annotations

from datetime import date, datetime, timedelta

from .constants import LOCAL_TZ


def _format_time(value: str | None) -> str:
    if not value:
        return "未知时间"

    return value.replace("T", " ").replace("+00:00", " UTC")


def _is_own_query(query: str) -> bool:
    return str(query or "").strip().lower() in {"", "我", "自己", "本人", "me", "self"}


def _parse_date_query(day: str) -> tuple[date | None, str]:
    normalized = str(day or "").strip().lower()
    today = datetime.now(LOCAL_TZ).date()
    if normalized in {"", "today", "今天", "今日"}:
        return today, "今天"
    if normalized in {"tomorrow", "明天", "明日"}:
        return today + timedelta(days=1), "明天"

    try:
        parsed = date.fromisoformat(normalized)
        return parsed, f"{parsed:%Y-%m-%d}"
    except ValueError:
        return None, ""
