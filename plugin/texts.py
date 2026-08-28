from __future__ import annotations


def _is_own_query(query: str) -> bool:
    return str(query or "").strip().lower() in {"", "我", "自己", "本人", "me", "self"}
