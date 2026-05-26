from __future__ import annotations

import re
import sqlite3
from datetime import datetime
from typing import Any

from .constants import LOCAL_TZ, MAX_EVENTS_PER_FILE
from .ics import _parse_ics_datetime_obj, _serialize_schedule_ics
from .time_utils import _now_iso

MAX_SQL_EDIT_CHANGES = 50
_ALLOWED_EDIT_SQL_RE = re.compile(r"^(update|insert|delete)\b", re.IGNORECASE)
_LOCAL_COURSES_RE = re.compile(r"\blocal_courses\b", re.IGNORECASE)
_FORBIDDEN_EDIT_SQL_RE = re.compile(
    r"\b(attach|alter|create|detach|drop|pragma|replace|select|vacuum)\b",
    re.IGNORECASE,
)


def _edit_schema_help() -> str:
    return "\n".join(
        [
            "请传入一条修改 local_courses 的 SQL，只支持 UPDATE、INSERT 或 DELETE。",
            "表结构：",
            "local_courses(id, course, location, description, dtstart, dtend, dtstart_tzid, dtend_tzid, rrule)",
            "",
            "字段说明：",
            "id 是课程事件序号，从 1 开始；修改或删除已有课程时必须用 WHERE id=... 精确指定。",
            "dtstart/dtend 使用 iCalendar 时间格式，例如 20260526T090000 或 20260526T090000Z。",
            "dtstart_tzid/dtend_tzid 可留空或填 Asia/Shanghai。",
            "",
            "示例：",
            "UPDATE local_courses SET course='高等数学', location='A101' WHERE id=2",
            "INSERT INTO local_courses(course, location, dtstart, dtend) VALUES ('高等数学', 'A101', '20260526T090000', '20260526T103000')",
            "DELETE FROM local_courses WHERE id=3",
        ]
    )


def _validate_edit_sql(sql: str) -> str:
    query = str(sql or "").strip()
    if not query:
        raise ValueError(_edit_schema_help())
    if ";" in query:
        raise ValueError("一次只能执行一条 SQL，不要包含分号。")
    if not _ALLOWED_EDIT_SQL_RE.match(query):
        raise ValueError("只支持 UPDATE、INSERT 或 DELETE。\n\n" + _edit_schema_help())
    if _FORBIDDEN_EDIT_SQL_RE.search(query):
        raise ValueError("SQL 中包含不允许的语句或关键字。\n\n" + _edit_schema_help())
    if not _LOCAL_COURSES_RE.search(query):
        raise ValueError("只能修改 local_courses 表。\n\n" + _edit_schema_help())
    return query


def _event_to_row(index: int, event: dict[str, str]) -> tuple[Any, ...]:
    return (
        index,
        event.get("UID") or "",
        event.get("SUMMARY") or "",
        event.get("LOCATION") or "",
        event.get("DESCRIPTION") or "",
        event.get("DTSTART") or "",
        event.get("DTEND") or "",
        event.get("DTSTART_TZID") or "",
        event.get("DTEND_TZID") or "",
        event.get("RRULE") or "",
        event.get("DTSTAMP") or "",
    )


def _row_to_event(row: sqlite3.Row) -> dict[str, str]:
    event: dict[str, str] = {}
    field_map = {
        "uid": "UID",
        "course": "SUMMARY",
        "location": "LOCATION",
        "description": "DESCRIPTION",
        "dtstart": "DTSTART",
        "dtend": "DTEND",
        "dtstart_tzid": "DTSTART_TZID",
        "dtend_tzid": "DTEND_TZID",
        "rrule": "RRULE",
        "dtstamp": "DTSTAMP",
    }
    for column, key in field_map.items():
        value = str(row[column] or "").strip()
        if value:
            event[key] = value
    return event


def _create_edit_db(events: list[dict[str, str]]) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE local_courses (
            id INTEGER PRIMARY KEY,
            uid TEXT,
            course TEXT NOT NULL,
            location TEXT,
            description TEXT,
            dtstart TEXT NOT NULL,
            dtend TEXT NOT NULL,
            dtstart_tzid TEXT,
            dtend_tzid TEXT,
            rrule TEXT,
            dtstamp TEXT
        )
        """
    )
    conn.executemany(
        """
        INSERT INTO local_courses
        (id, uid, course, location, description, dtstart, dtend, dtstart_tzid, dtend_tzid, rrule, dtstamp)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [_event_to_row(index, event) for index, event in enumerate(events, start=1)],
    )
    return conn


def _validate_events(rows: list[sqlite3.Row]) -> list[dict[str, str]]:
    if len(rows) > MAX_EVENTS_PER_FILE:
        raise ValueError(f"修改后课程数量超过上限 {MAX_EVENTS_PER_FILE}。")

    events = [_row_to_event(row) for row in rows]
    for index, event in enumerate(events, start=1):
        if not event.get("SUMMARY"):
            raise ValueError(f"id={index} 缺少 course。")
        if not event.get("DTSTART"):
            raise ValueError(f"id={index} 缺少 dtstart。")
        if not event.get("DTEND"):
            raise ValueError(f"id={index} 缺少 dtend。")
        start = _parse_ics_datetime_obj(event["DTSTART"], event.get("DTSTART_TZID"))
        end = _parse_ics_datetime_obj(event["DTEND"], event.get("DTEND_TZID"))
        if not start:
            raise ValueError(f"id={index} 的 dtstart 无法解析。")
        if not end:
            raise ValueError(f"id={index} 的 dtend 无法解析。")
        if end <= start:
            raise ValueError(f"id={index} 的 dtend 必须晚于 dtstart。")

    events.sort(key=lambda item: item.get("DTSTART", ""))
    return events


def apply_local_course_sql_edit(
    member_info: dict[str, Any], sql: str
) -> tuple[list[dict[str, str]], str, int]:
    query = _validate_edit_sql(sql)
    raw_events = member_info.get("events")
    if not isinstance(raw_events, list) or not raw_events:
        raise ValueError("当前课程表没有可编辑的 .ics 事件。")

    events = [dict(event) for event in raw_events if isinstance(event, dict)]
    conn = _create_edit_db(events)
    before = conn.total_changes
    try:
        conn.execute(query)
        changes = conn.total_changes - before
        if changes <= 0:
            raise ValueError("SQL 没有修改任何课程。")
        if changes > MAX_SQL_EDIT_CHANGES:
            raise ValueError(f"一次最多允许修改 {MAX_SQL_EDIT_CHANGES} 条课程。")
        rows = conn.execute(
            "SELECT * FROM local_courses ORDER BY dtstart, dtend, id"
        ).fetchall()
        edited_events = _validate_events(rows)
    except sqlite3.Error as exc:
        raise ValueError(f"SQL 修改失败：{exc}\n\n{_edit_schema_help()}") from exc
    finally:
        conn.close()

    now = datetime.now(LOCAL_TZ)
    dtstamp = now.astimezone(LOCAL_TZ).strftime("%Y%m%dT%H%M%S")
    for event in edited_events:
        event["DTSTAMP"] = event.get("DTSTAMP") or dtstamp

    ics_content = _serialize_schedule_ics(edited_events)
    return edited_events, ics_content, changes


def apply_sql_edit_to_member(member_info: dict[str, Any], sql: str) -> dict[str, Any]:
    events, ics_content, changes = apply_local_course_sql_edit(member_info, sql)
    now = _now_iso()
    copied = dict(member_info)
    copied["events"] = events
    copied["ics"] = ics_content
    copied["event_count"] = len(events)
    copied["source"] = "ics"
    copied["updated_at"] = now
    copied["schedule_updated_at"] = now
    copied["content_updated_at"] = now
    copied["last_modified_at"] = now
    copied["_sql_edit_changes"] = changes
    return copied
