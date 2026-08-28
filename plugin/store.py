from __future__ import annotations

from astrbot.api.event import AstrMessageEvent


def _scope_id(event: AstrMessageEvent) -> str:
    group_id = event.get_group_id()
    if group_id:
        return f"group:{group_id}"

    return f"private:{event.get_sender_id()}"
