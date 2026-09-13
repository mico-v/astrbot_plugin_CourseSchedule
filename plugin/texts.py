from __future__ import annotations


def _is_own_query(query: str) -> bool:
    return str(query or "").strip().lower() in {"", "我", "自己", "本人", "me", "self"}


def _command_tail(event, value: str = "") -> str:
    """Return the text after the command name.

    AstrBot binds only the first word after the command to a plain ``str``
    parameter, so a range like ``2026-09-01 .. 2026-09-30`` reaches the handler
    as its first token only.  Reading the raw message picks up the rest.
    """
    raw = str(value or "").strip()
    if raw:
        return raw
    try:
        getter = getattr(event, "get_message_str", None)
        message = str(getter() if callable(getter) else getattr(event, "message_str", "") or "")
    except Exception:
        return ""
    parts = message.strip().split(maxsplit=1)
    return parts[1].strip() if len(parts) == 2 else ""
