from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from _plugin_loader import load_plugin_module

sqlite_store = load_plugin_module("course_schedule_store_test_plugin", "sqlite_store")
SQLiteScheduleStore = sqlite_store.SQLiteScheduleStore
ScheduleWriteConflict = sqlite_store.ScheduleWriteConflict


class SQLiteScheduleStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "course_schedule.sqlite3"

    async def asyncTearDown(self) -> None:
        self.temp_dir.cleanup()

    async def test_initializes_empty_database_and_stores_structured_events(self) -> None:
        store = SQLiteScheduleStore(db_path=self.db_path)
        self.assertEqual(await store.get_scope_members("group:1"), {})
        await store.put_member(
            "group:1",
            "10",
            {
                "name": "张三",
                "schedule": "高等数学",
                "events": [{"UID": "course-1", "SUMMARY": "高等数学"}],
                "ics": "BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n",
                "updated_at": "2026-08-28T00:00:00+00:00",
            },
            expected_revision=0,
        )
        members = await store.get_scope_members("group:1")
        self.assertEqual(members["10"]["name"], "张三")
        self.assertEqual(members["10"]["events"][0]["UID"], "course-1")
        with sqlite3.connect(self.db_path) as conn:
            event_row = conn.execute(
                "SELECT uid, summary FROM course_events WHERE scope_id='group:1' AND user_id='10'"
            ).fetchone()
        self.assertEqual(event_row, ("course-1", "高等数学"))

    async def test_member_writes_do_not_replace_other_members(self) -> None:
        store = SQLiteScheduleStore(db_path=self.db_path)
        await store.put_member("group:1", "10", {"name": "张三", "updated_at": "1"})
        await store.put_member("group:1", "20", {"name": "李四", "updated_at": "1"})
        await store.put_member("group:1", "10", {"name": "张三新", "updated_at": "1"})

        members = await store.get_scope_members("group:1")
        self.assertEqual(set(members), {"10", "20"})
        self.assertEqual(members["10"]["name"], "张三新")
        self.assertEqual(members["20"]["name"], "李四")

    async def test_optimistic_revision_rejects_stale_write(self) -> None:
        store = SQLiteScheduleStore(db_path=self.db_path)
        await store.put_member("group:1", "10", {"name": "张三"})
        first = await store.get_member("group:1", "10")
        stale = dict(first or {})
        await store.put_member("group:1", "10", {"name": "已更新"})

        with self.assertRaises(ScheduleWriteConflict):
            await store.put_member(
                "group:1",
                "10",
                stale,
                expected_revision=int(stale["_revision"]),
            )

    async def test_create_revision_rejects_concurrent_create(self) -> None:
        store = SQLiteScheduleStore(db_path=self.db_path)
        await store.put_member(
            "group:1", "10", {"name": "第一个"}, expected_revision=0
        )
        with self.assertRaises(ScheduleWriteConflict):
            await store.put_member(
                "group:1", "10", {"name": "第二个"}, expected_revision=0
            )

    async def test_lists_scope_summaries_for_webui(self) -> None:
        store = SQLiteScheduleStore(db_path=self.db_path)
        await store.put_member(
            "group:2",
            "20",
            {
                "name": "李四",
                "events": [{"UID": "course-1", "SUMMARY": "英语"}],
            },
        )
        await store.put_member("group:1", "10", {"name": "张三"})

        summaries = await store.list_scope_summaries()

        self.assertEqual(
            [item["scope_id"] for item in summaries], ["group:1", "group:2"]
        )
        self.assertEqual(summaries[1]["member_count"], 1)
        self.assertEqual(summaries[1]["event_count"], 1)
        self.assertEqual(summaries[1]["members"][0]["name"], "李四")

    async def test_replace_day_overrides_rewrites_only_given_members(self) -> None:
        """A backup restore replaces the markers it carries and keeps the rest."""
        store = SQLiteScheduleStore(db_path=self.db_path)
        await store.set_day_override(
            "group:1", "*", "2026-10-01", "holiday", created_by="10"
        )
        await store.set_day_override(
            "group:1", "10", "2026-10-11", "shift", source_day="2026-10-08"
        )
        await store.set_day_override("group:1", "20", "2026-12-25", "holiday")
        await store.set_day_override("group:2", "10", "2026-10-11", "holiday")

        count = await store.replace_day_overrides(
            "group:1",
            {"*", "10"},
            [
                {
                    "user_id": "10",
                    "day": "2026-10-12",
                    "kind": "holiday",
                    "source_day": "",
                    "created_by": "10",
                    "created_at": "2026-09-21T00:00:00+00:00",
                }
            ],
        )

        self.assertEqual(count, 1)
        self.assertEqual(
            [
                (row["user_id"], row["day"], row["kind"])
                for row in await store.list_day_overrides("group:1")
            ],
            [("10", "2026-10-12", "holiday"), ("20", "2026-12-25", "holiday")],
        )
        # Another scope is never touched.
        self.assertEqual(len(await store.list_day_overrides("group:2")), 1)

    async def test_replace_day_overrides_can_clear_every_marker(self) -> None:
        store = SQLiteScheduleStore(db_path=self.db_path)
        await store.set_day_override("group:1", "*", "2026-10-01", "holiday")
        await store.set_day_override("group:1", "10", "2026-10-11", "holiday")

        await store.replace_day_overrides("group:1", {"*", "10"}, [])

        self.assertEqual(await store.list_day_overrides("group:1"), [])


if __name__ == "__main__":
    unittest.main()
