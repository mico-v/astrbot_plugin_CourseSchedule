"""ICS import boundary: who may import whose schedule, and under what name."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from _plugin_loader import load_plugin_module

PACKAGE = "course_schedule_import_test_plugin"
course_schedule = load_plugin_module(PACKAGE, "course_schedule")
sqlite_store = load_plugin_module(PACKAGE, "sqlite_store")

SAMPLE_ICS = """BEGIN:VCALENDAR\r
VERSION:2.0\r
BEGIN:VEVENT\r
UID:course-1\r
DTSTART;TZID=Asia/Shanghai:20260831T090000\r
DTEND;TZID=Asia/Shanghai:20260831T103000\r
SUMMARY:高等数学\r
END:VEVENT\r
END:VCALENDAR\r
"""


class FakeAt:
    def __init__(self, qq: str, name: str = ""):
        self.qq = qq
        self.name = name


class FakeEvent:
    def __init__(
        self,
        sender_id: str,
        *,
        group_id: str = "1",
        admin: bool = False,
        messages: list[object] | None = None,
    ):
        self.sender_id = sender_id
        self.group_id = group_id
        self.admin = admin
        self.messages = messages or []

    def get_sender_id(self) -> str:
        return self.sender_id

    def get_sender_name(self) -> str:
        return {"100": "小明", "200": "小红"}.get(self.sender_id, self.sender_id)

    def get_group_id(self) -> str:
        return self.group_id

    def get_messages(self) -> list[object]:
        return self.messages

    def is_admin(self) -> bool:
        return self.admin


class IcsImportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        base = course_schedule.CourseScheduleBase.__new__(
            course_schedule.CourseScheduleBase
        )
        base._schedule_store = sqlite_store.SQLiteScheduleStore(
            db_path=Path(self.temp_dir.name) / "schedule.sqlite3"
        )
        self.service = base

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _member(self, user_id: str):
        return asyncio.run(self.service._schedule_store.get_member("group:1", user_id))

    def test_imports_for_the_sender_under_their_own_name(self) -> None:
        async def scenario() -> None:
            result = await self.service._save_ics_schedule(
                FakeEvent("100"), SAMPLE_ICS, source_file="schedule.ics"
            )
            self.assertIn("已保存", result)
            info = await self.service._schedule_store.get_member("group:1", "100")
            self.assertEqual(info["name"], "小明")
            self.assertEqual(info["uploader_id"], "100")
            self.assertEqual(len(info["events"]), 1)

        asyncio.run(scenario())

    def test_another_members_file_needs_admin_rights(self) -> None:
        """`schedule<QQ号>.ics` addresses someone else's row, so it is an edit."""

        async def scenario() -> None:
            denied = await self.service._save_ics_schedule(
                FakeEvent("100"), SAMPLE_ICS, user_id="200", source_file="schedule200.ics"
            )
            self.assertIn("需要管理员权限", denied)
            self.assertIsNone(await self.service._schedule_store.get_member("group:1", "200"))

            allowed = await self.service._save_ics_schedule(
                FakeEvent("100", admin=True),
                SAMPLE_ICS,
                user_id="200",
                source_file="schedule200.ics",
            )
            self.assertIn("已保存", allowed)
            info = await self.service._schedule_store.get_member("group:1", "200")
            self.assertEqual(len(info["events"]), 1)

        asyncio.run(scenario())

    def test_private_chat_cannot_import_for_someone_else(self) -> None:
        async def scenario() -> None:
            result = await self.service._save_ics_schedule(
                FakeEvent("100", group_id="", admin=True),
                SAMPLE_ICS,
                user_id="200",
                source_file="schedule200.ics",
            )
            self.assertIn("私聊只能导入自己的课表", result)

        asyncio.run(scenario())

    def test_importing_for_another_member_never_uses_the_uploaders_nickname(self) -> None:
        async def scenario() -> None:
            # The uploader is 小明(100); the row belongs to 200 and the file
            # carries no name for them, so the QQ号 is the only safe label.
            await self.service._save_ics_schedule(
                FakeEvent("100", admin=True),
                SAMPLE_ICS,
                user_id="200",
                source_file="schedule200.ics",
            )
            info = await self.service._schedule_store.get_member("group:1", "200")
            self.assertEqual(info["name"], "200")

        asyncio.run(scenario())

    def test_an_existing_name_survives_a_reimport(self) -> None:
        async def scenario() -> None:
            await self.service._save_ics_schedule(
                FakeEvent("100", admin=True),
                SAMPLE_ICS,
                user_id="200",
                source_file="schedule200.ics",
            )
            await self.service._edit_schedule_text(
                FakeEvent("100", admin=True),
                "update",
                person="200",
                course_id=0,
                member_name="小红",
            )
            await self.service._save_ics_schedule(
                FakeEvent("100", admin=True),
                SAMPLE_ICS,
                user_id="200",
                source_file="schedule200.ics",
            )
            info = await self.service._schedule_store.get_member("group:1", "200")
            self.assertEqual(info["name"], "小红")

        asyncio.run(scenario())

    def test_rejects_content_without_vevent(self) -> None:
        async def scenario() -> None:
            result = await self.service._save_ics_schedule(
                FakeEvent("100"), "BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n"
            )
            self.assertIn("没有 VEVENT", result)

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
