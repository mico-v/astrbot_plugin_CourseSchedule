from __future__ import annotations

from datetime import datetime
from typing import Any

from astrbot.api.event import AstrMessageEvent

from .time_utils import _parse_timestamp


def _empty_store() -> dict[str, Any]:
    return {"version": 1, "groups": {}}


def _normalize_store(raw_store: Any) -> dict[str, Any]:
    if not isinstance(raw_store, dict):
        return _empty_store()

    groups = raw_store.get("groups")
    if not isinstance(groups, dict):
        raw_store["groups"] = {}

    raw_store.setdefault("version", 1)
    return raw_store


def _scope_id(event: AstrMessageEvent) -> str:
    group_id = event.get_group_id()
    if group_id:
        return f"group:{group_id}"

    return f"private:{event.get_sender_id()}"


def _scope_label(event: AstrMessageEvent) -> str:
    group_id = event.get_group_id()
    return f"群 {group_id}" if group_id else "私聊"


def _group_file_updated_at(file_info: dict[str, Any]) -> datetime | None:
    timestamp_keys = (
        "modify_time",
        "modified_time",
        "update_time",
        "updated_at",
        "mtime",
        "upload_time",
        "created_at",
        "create_time",
        "ctime",
        "time",
    )
    for key in timestamp_keys:
        parsed = _parse_timestamp(file_info.get(key))
        if parsed:
            return parsed

    return None


def _local_schedule_updated_at(info: dict[str, Any]) -> datetime | None:
    return (
        _parse_timestamp(info.get("schedule_updated_at"))
        or _parse_timestamp(info.get("content_updated_at"))
        or _parse_timestamp(info.get("updated_at"))
    )


def _compare_timestamps(left: datetime | None, right: datetime | None) -> int | None:
    if not left or not right:
        return None

    delta = (left - right).total_seconds()
    if abs(delta) <= 2:
        return 0
    return 1 if delta > 0 else -1
