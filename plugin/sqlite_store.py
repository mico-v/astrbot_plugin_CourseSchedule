from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from typing import Any

from .constants import PLUGIN_ID


SCHEMA_VERSION = 1


def _plugin_data_dir() -> Path:
    """Return AstrBot's persistent plugin-data directory."""
    try:
        from astrbot.api.star import StarTools

        return Path(StarTools.get_data_dir(PLUGIN_ID))
    except Exception:
        # Fallback keeps development/test environments usable and follows the
        # documented AstrBot layout.
        from astrbot.core.utils.astrbot_path import get_astrbot_data_path

        path = Path(get_astrbot_data_path()) / "plugin_data" / PLUGIN_ID
        path.mkdir(parents=True, exist_ok=True)
        return path


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
            if key not in {"_revision", "events"}
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
                members[str(row["user_id"])] = info
        return members

    async def get_scope_members(self, scope_id: str) -> dict[str, Any]:
        await self.ensure_initialized()
        async with self._lock:
            return self._get_scope_members_sync(scope_id)

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
        return info

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

    def _patch_member_sync(
        self, scope_id: str, user_id: str, changes: dict[str, Any]
    ) -> bool:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT data_json FROM schedule_members
                WHERE scope_id = ? AND user_id = ?
                """,
                (scope_id, user_id),
            ).fetchone()
            if not row:
                conn.rollback()
                return False
            try:
                info = json.loads(row["data_json"])
            except (TypeError, json.JSONDecodeError):
                info = {}
            if not isinstance(info, dict):
                info = {}
            info.update(changes)
            events_changed = "events" in changes
            if events_changed:
                payload, events = self._serialize_member(info)
            else:
                payload = json.dumps(info, ensure_ascii=False, separators=(",", ":"))
                events = []
            conn.execute(
                """
                UPDATE schedule_members
                SET data_json = ?, updated_at = ?, revision = revision + 1
                WHERE scope_id = ? AND user_id = ?
                """,
                (payload, str(info.get("updated_at") or ""), scope_id, user_id),
            )
            if events_changed:
                self._replace_events(conn, scope_id, user_id, events)
            conn.commit()
            return True

    async def patch_member(
        self, scope_id: str, user_id: str, changes: dict[str, Any]
    ) -> bool:
        await self.ensure_initialized()
        async with self._lock:
            return self._patch_member_sync(scope_id, user_id, changes)

    def _delete_member_sync(self, scope_id: str, user_id: str) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM schedule_members WHERE scope_id = ? AND user_id = ?",
                (scope_id, user_id),
            )
            conn.commit()
            return cursor.rowcount == 1

    async def delete_member(self, scope_id: str, user_id: str) -> bool:
        await self.ensure_initialized()
        async with self._lock:
            return self._delete_member_sync(scope_id, user_id)
