from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from typing import Any

from .constants import DAY_OVERRIDE_ALL
from .files import _plugin_data_dir


SCHEMA_VERSION = 2


class ScheduleWriteConflict(RuntimeError):
    pass


class SQLiteScheduleStore:
    """Transactional SQLite persistence for course schedules."""

    def __init__(
        self,
        db_path: Path | None = None,
    ) -> None:
        self._lock = asyncio.Lock()
        self._initialized = False
        self.path = db_path or (_plugin_data_dir() / "course_schedule.sqlite3")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 30000")
        return conn

    def _initialize_sync(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS schedule_members (
                    scope_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    data_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT '',
                    revision INTEGER NOT NULL DEFAULT 1,
                    PRIMARY KEY (scope_id, user_id)
                );
                CREATE INDEX IF NOT EXISTS idx_schedule_members_scope
                    ON schedule_members(scope_id);
                CREATE TABLE IF NOT EXISTS course_events (
                    scope_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    event_index INTEGER NOT NULL,
                    uid TEXT NOT NULL DEFAULT '',
                    summary TEXT NOT NULL DEFAULT '',
                    location TEXT NOT NULL DEFAULT '',
                    description TEXT NOT NULL DEFAULT '',
                    dtstart TEXT NOT NULL DEFAULT '',
                    dtend TEXT NOT NULL DEFAULT '',
                    dtstart_tzid TEXT NOT NULL DEFAULT '',
                    dtend_tzid TEXT NOT NULL DEFAULT '',
                    rrule TEXT NOT NULL DEFAULT '',
                    dtstamp TEXT NOT NULL DEFAULT '',
                    event_json TEXT NOT NULL,
                    PRIMARY KEY (scope_id, user_id, event_index),
                    FOREIGN KEY (scope_id, user_id)
                        REFERENCES schedule_members(scope_id, user_id)
                        ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_course_events_scope_start
                    ON course_events(scope_id, dtstart);
                CREATE INDEX IF NOT EXISTS idx_course_events_member_uid
                    ON course_events(scope_id, user_id, uid);
                CREATE TABLE IF NOT EXISTS schedule_day_overrides (
                    scope_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    day TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    source_day TEXT NOT NULL DEFAULT '',
                    created_by TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY (scope_id, user_id, day)
                );
                CREATE INDEX IF NOT EXISTS idx_schedule_day_overrides_scope
                    ON schedule_day_overrides(scope_id, day);
                """
            )
            conn.execute(
                "INSERT OR REPLACE INTO metadata(key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )

    def _serialize_member(self, info: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
        clean_info = {
            key: value
            for key, value in info.items()
            if key not in {"_revision", "_day_overrides", "events"}
        }
        raw_events = info.get("events")
        events = (
            [dict(event) for event in raw_events if isinstance(event, dict)]
            if isinstance(raw_events, list)
            else []
        )
        clean_info["event_count"] = len(events)
        return json.dumps(clean_info, ensure_ascii=False, separators=(",", ":")), events

    def _replace_events(
        self,
        conn: sqlite3.Connection,
        scope_id: str,
        user_id: str,
        events: list[dict[str, Any]],
    ) -> None:
        conn.execute(
            "DELETE FROM course_events WHERE scope_id = ? AND user_id = ?",
            (scope_id, user_id),
        )
        rows = []
        for index, event in enumerate(events, start=1):
            rows.append(
                (
                    scope_id,
                    user_id,
                    index,
                    str(event.get("UID") or ""),
                    str(event.get("SUMMARY") or ""),
                    str(event.get("LOCATION") or ""),
                    str(event.get("DESCRIPTION") or ""),
                    str(event.get("DTSTART") or ""),
                    str(event.get("DTEND") or ""),
                    str(event.get("DTSTART_TZID") or ""),
                    str(event.get("DTEND_TZID") or ""),
                    str(event.get("RRULE") or ""),
                    str(event.get("DTSTAMP") or ""),
                    json.dumps(event, ensure_ascii=False, separators=(",", ":")),
                )
            )
        if rows:
            conn.executemany(
                """
                INSERT INTO course_events (
                    scope_id, user_id, event_index, uid, summary, location,
                    description, dtstart, dtend, dtstart_tzid, dtend_tzid,
                    rrule, dtstamp, event_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )

    def _decode_event_rows(self, rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        for row in rows:
            try:
                event = json.loads(row["event_json"])
            except (TypeError, json.JSONDecodeError):
                event = {}
            if isinstance(event, dict):
                events.append(event)
        return events

    async def ensure_initialized(self) -> None:
        if self._initialized:
            return
        async with self._lock:
            if self._initialized:
                return
            self._initialize_sync()
            self._initialized = True

    def _get_scope_members_sync(self, scope_id: str) -> dict[str, Any]:
        members: dict[str, Any] = {}
        scope_overrides = self._scope_day_overrides_sync(scope_id)
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT user_id, data_json, revision
                FROM schedule_members WHERE scope_id = ? ORDER BY user_id
                """,
                (scope_id,),
            ).fetchall()
            event_rows = conn.execute(
                """
                SELECT user_id, event_json FROM course_events
                WHERE scope_id = ? ORDER BY user_id, event_index
                """,
                (scope_id,),
            ).fetchall()
        events_by_user: dict[str, list[dict[str, Any]]] = {}
        for row in event_rows:
            events_by_user.setdefault(str(row["user_id"]), []).extend(
                self._decode_event_rows([row])
            )
        for row in rows:
            try:
                info = json.loads(row["data_json"])
            except (TypeError, json.JSONDecodeError):
                continue
            if isinstance(info, dict):
                info["_revision"] = int(row["revision"])
                info["events"] = events_by_user.get(str(row["user_id"]), [])
                info["event_count"] = len(info["events"])
                members[str(row["user_id"])] = self._attach_day_overrides(
                    info, str(row["user_id"]), scope_overrides
                )
        return members

    async def get_scope_members(self, scope_id: str) -> dict[str, Any]:
        await self.ensure_initialized()
        async with self._lock:
            return self._get_scope_members_sync(scope_id)

    def _list_scope_summaries_sync(self) -> list[dict[str, Any]]:
        summaries: dict[str, dict[str, Any]] = {}
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT scope_id, user_id, data_json, revision
                FROM schedule_members
                ORDER BY scope_id, user_id
                """
            ).fetchall()
        for row in rows:
            scope_id = str(row["scope_id"])
            summary = summaries.setdefault(
                scope_id,
                {
                    "scope_id": scope_id,
                    "members": [],
                    "member_count": 0,
                    "event_count": 0,
                },
            )
            try:
                info = json.loads(row["data_json"])
            except (TypeError, json.JSONDecodeError):
                info = {}
            if not isinstance(info, dict):
                info = {}
            events = info.get("events")
            event_count = int(info.get("event_count") or 0)
            if isinstance(events, list):
                event_count = len(events)
            member = {
                "user_id": str(row["user_id"]),
                "name": str(info.get("name") or row["user_id"]),
                "event_count": event_count,
                "revision": int(row["revision"]),
            }
            summary["members"].append(member)
            summary["member_count"] += 1
            summary["event_count"] += event_count
        return list(summaries.values())

    async def list_scope_summaries(self) -> list[dict[str, Any]]:
        await self.ensure_initialized()
        async with self._lock:
            return self._list_scope_summaries_sync()

    @staticmethod
    def _merge_day_overrides(
        scope_overrides: dict[str, dict[str, dict[str, str]]], user_id: str
    ) -> dict[str, dict[str, str]]:
        """Member rules win over the scope-wide all-members rules."""
        merged = dict(scope_overrides.get(DAY_OVERRIDE_ALL) or {})
        merged.update(scope_overrides.get(str(user_id)) or {})
        return merged

    @classmethod
    def _attach_day_overrides(
        cls,
        info: dict[str, Any],
        user_id: str,
        scope_overrides: dict[str, dict[str, dict[str, str]]],
    ) -> dict[str, Any]:
        info["_day_overrides"] = cls._merge_day_overrides(scope_overrides, user_id)
        return info

    def _scope_day_overrides_sync(
        self, scope_id: str
    ) -> dict[str, dict[str, dict[str, str]]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT user_id, day, kind, source_day, created_by, created_at
                FROM schedule_day_overrides WHERE scope_id = ?
                ORDER BY day, user_id
                """,
                (scope_id,),
            ).fetchall()
        overrides: dict[str, dict[str, dict[str, str]]] = {}
        for row in rows:
            overrides.setdefault(str(row["user_id"]), {})[str(row["day"])] = {
                "kind": str(row["kind"]),
                "source_day": str(row["source_day"]),
                "created_by": str(row["created_by"]),
                "created_at": str(row["created_at"]),
            }
        return overrides

    def _list_day_overrides_sync(self, scope_id: str) -> list[dict[str, str]]:
        """Flat marker list for one scope, soonest day first."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT user_id, day, kind, source_day, created_by, created_at
                FROM schedule_day_overrides WHERE scope_id = ?
                ORDER BY day, user_id
                """,
                (scope_id,),
            ).fetchall()
        return [
            {
                "user_id": str(row["user_id"]),
                "day": str(row["day"]),
                "kind": str(row["kind"]),
                "source_day": str(row["source_day"]),
                "created_by": str(row["created_by"]),
                "created_at": str(row["created_at"]),
            }
            for row in rows
        ]

    async def list_day_overrides(self, scope_id: str) -> list[dict[str, str]]:
        await self.ensure_initialized()
        async with self._lock:
            return self._list_day_overrides_sync(scope_id)

    def _set_day_override_sync(
        self,
        scope_id: str,
        user_id: str,
        day: str,
        kind: str,
        source_day: str = "",
        created_by: str = "",
        created_at: str = "",
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO schedule_day_overrides
                    (scope_id, user_id, day, kind, source_day, created_by, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(scope_id, user_id, day) DO UPDATE SET
                    kind = excluded.kind,
                    source_day = excluded.source_day,
                    created_by = excluded.created_by,
                    created_at = excluded.created_at
                """,
                (scope_id, user_id, day, kind, source_day, created_by, created_at),
            )
            conn.commit()

    async def set_day_override(
        self,
        scope_id: str,
        user_id: str,
        day: str,
        kind: str,
        source_day: str = "",
        created_by: str = "",
        created_at: str = "",
    ) -> None:
        await self.ensure_initialized()
        async with self._lock:
            self._set_day_override_sync(
                scope_id, user_id, day, kind, source_day, created_by, created_at
            )

    def _delete_day_override_sync(self, scope_id: str, user_id: str, day: str) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                DELETE FROM schedule_day_overrides
                WHERE scope_id = ? AND user_id = ? AND day = ?
                """,
                (scope_id, user_id, day),
            )
            conn.commit()
            return cursor.rowcount == 1

    async def delete_day_override(
        self, scope_id: str, user_id: str, day: str
    ) -> bool:
        await self.ensure_initialized()
        async with self._lock:
            return self._delete_day_override_sync(scope_id, user_id, day)

    def _replace_day_overrides_sync(
        self,
        scope_id: str,
        user_ids: set[str],
        rows: list[dict[str, Any]],
    ) -> int:
        """Make the markers of ``user_ids`` match ``rows`` exactly.

        Markers belonging to other members are left alone, so restoring a
        backup only rewrites the members the backup actually contains.  The
        whole rewrite is one transaction: a restore either lands completely or
        not at all.
        """
        wanted = {
            (str(row.get("user_id") or ""), str(row.get("day") or ""))
            for row in rows
        }
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                """
                SELECT user_id, day FROM schedule_day_overrides WHERE scope_id = ?
                """,
                (scope_id,),
            ).fetchall()
            for row in existing:
                user_id = str(row["user_id"])
                day = str(row["day"])
                if user_id not in user_ids or (user_id, day) in wanted:
                    continue
                conn.execute(
                    """
                    DELETE FROM schedule_day_overrides
                    WHERE scope_id = ? AND user_id = ? AND day = ?
                    """,
                    (scope_id, user_id, day),
                )
            for row in rows:
                conn.execute(
                    """
                    INSERT INTO schedule_day_overrides
                        (scope_id, user_id, day, kind, source_day, created_by, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(scope_id, user_id, day) DO UPDATE SET
                        kind = excluded.kind,
                        source_day = excluded.source_day,
                        created_by = excluded.created_by,
                        created_at = excluded.created_at
                    """,
                    (
                        scope_id,
                        str(row.get("user_id") or ""),
                        str(row.get("day") or ""),
                        str(row.get("kind") or ""),
                        str(row.get("source_day") or ""),
                        str(row.get("created_by") or ""),
                        str(row.get("created_at") or ""),
                    ),
                )
            conn.commit()
        return len(rows)

    async def replace_day_overrides(
        self,
        scope_id: str,
        user_ids: set[str],
        rows: list[dict[str, Any]],
    ) -> int:
        await self.ensure_initialized()
        async with self._lock:
            return self._replace_day_overrides_sync(scope_id, user_ids, rows)

    def _get_member_sync(self, scope_id: str, user_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT data_json, revision FROM schedule_members
                WHERE scope_id = ? AND user_id = ?
                """,
                (scope_id, user_id),
            ).fetchone()
            event_rows = conn.execute(
                """
                SELECT event_json FROM course_events
                WHERE scope_id = ? AND user_id = ? ORDER BY event_index
                """,
                (scope_id, user_id),
            ).fetchall()
        if not row:
            return None
        try:
            info = json.loads(row["data_json"])
        except (TypeError, json.JSONDecodeError):
            return None
        if not isinstance(info, dict):
            return None
        info["_revision"] = int(row["revision"])
        info["events"] = self._decode_event_rows(event_rows)
        info["event_count"] = len(info["events"])
        return self._attach_day_overrides(
            info, user_id, self._scope_day_overrides_sync(scope_id)
        )

    async def get_member(self, scope_id: str, user_id: str) -> dict[str, Any] | None:
        await self.ensure_initialized()
        async with self._lock:
            return self._get_member_sync(scope_id, user_id)

    def _put_member_sync(
        self,
        scope_id: str,
        user_id: str,
        info: dict[str, Any],
        expected_revision: int | None,
    ) -> None:
        payload, events = self._serialize_member(info)
        updated_at = str(info.get("updated_at") or "")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if expected_revision is None:
                conn.execute(
                    """
                    INSERT INTO schedule_members
                        (scope_id, user_id, data_json, updated_at, revision)
                    VALUES (?, ?, ?, ?, 1)
                    ON CONFLICT(scope_id, user_id) DO UPDATE SET
                        data_json = excluded.data_json,
                        updated_at = excluded.updated_at,
                        revision = schedule_members.revision + 1
                    """,
                    (scope_id, user_id, payload, updated_at),
                )
            elif expected_revision == 0:
                try:
                    conn.execute(
                        """
                        INSERT INTO schedule_members
                            (scope_id, user_id, data_json, updated_at, revision)
                        VALUES (?, ?, ?, ?, 1)
                        """,
                        (scope_id, user_id, payload, updated_at),
                    )
                except sqlite3.IntegrityError as exc:
                    conn.rollback()
                    raise ScheduleWriteConflict(
                        "课程表已被其他请求创建，请重新操作。"
                    ) from exc
            else:
                cursor = conn.execute(
                    """
                    UPDATE schedule_members
                    SET data_json = ?, updated_at = ?, revision = revision + 1
                    WHERE scope_id = ? AND user_id = ? AND revision = ?
                    """,
                    (payload, updated_at, scope_id, user_id, expected_revision),
                )
                if cursor.rowcount != 1:
                    conn.rollback()
                    raise ScheduleWriteConflict("课程表已被其他请求更新，请重新操作。")
            self._replace_events(conn, scope_id, user_id, events)
            conn.commit()

    async def put_member(
        self,
        scope_id: str,
        user_id: str,
        info: dict[str, Any],
        expected_revision: int | None = None,
    ) -> None:
        await self.ensure_initialized()
        async with self._lock:
            self._put_member_sync(scope_id, user_id, info, expected_revision)
