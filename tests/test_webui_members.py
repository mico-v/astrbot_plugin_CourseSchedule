from __future__ import annotations

import asyncio
import tempfile
import types
import unittest
from pathlib import Path

from _plugin_loader import load_plugin_module

PACKAGE = "course_schedule_webui_test_plugin"
course_schedule = load_plugin_module(PACKAGE, "course_schedule")
sqlite_store = load_plugin_module(PACKAGE, "sqlite_store")


class FakeClient:
    """Stands in for a OneBot client exposing ``api.call_action``."""

    def __init__(self, platform: "FakePlatform"):
        self.platform = platform
        self.api = self

    async def call_action(self, action: str, **params):
        self.platform.calls.append((action, params))
        if self.platform.error:
            raise RuntimeError(self.platform.error)
        return [dict(member) for member in self.platform.members]


class FakePlatform:
    def __init__(self, name: str, members=None, error: str = ""):
        self._name = name
        self.members = members or []
        self.error = error
        self.calls: list[tuple[str, dict]] = []
        self.client = FakeClient(self)

    def meta(self):
        return types.SimpleNamespace(name=self._name)

    def get_client(self):
        return self.client


class FakeContext:
    def __init__(self, platforms: list[FakePlatform]):
        self.platform_manager = types.SimpleNamespace(get_insts=lambda: platforms)


def _member(user_id, nickname="", card="", role="member"):
    return {
        "group_id": 1,
        "user_id": user_id,
        "nickname": nickname,
        "card": card,
        "role": role,
    }


class GroupMemberLookupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.service = course_schedule.CourseScheduleBase.__new__(
            course_schedule.CourseScheduleBase
        )
        self.service._schedule_store = sqlite_store.SQLiteScheduleStore(
            db_path=Path(self.temp_dir.name) / "schedule.sqlite3"
        )
        self.group = FakePlatform(
            "aiocqhttp",
            members=[
                _member(100, nickname="小明", card="小明"),
                _member(200, nickname="小红"),
                _member(300, nickname="小刚", role="admin"),
            ],
        )
        self.service.context = FakeContext([self.group])

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_normalizes_names_and_skips_unusable_ids(self) -> None:
        rows = course_schedule.CourseScheduleBase._normalize_group_members(
            [
                _member(100, nickname="昵称", card="群名片"),
                _member("", nickname="空号"),
                _member("abc", nickname="非数字"),
                _member(100, nickname="重复"),
                "junk",
            ]
        )
        self.assertEqual(
            rows, [{"user_id": "100", "name": "群名片", "role": "member"}]
        )

    def test_reads_members_from_the_platform_and_filters_existing(self) -> None:
        async def scenario() -> None:
            await self.service._create_member_schedules(
                "group:1", [{"user_id": "200", "name": "小红"}]
            )
            pending = await self.service._pending_group_members("group:1")
            self.assertEqual([item["user_id"] for item in pending], ["100", "300"])
            action, params = self.group.calls[-1]
            self.assertEqual(action, "get_group_member_list")
            self.assertEqual(params, {"group_id": 1})

        asyncio.run(scenario())

    def test_falls_back_to_another_platform(self) -> None:
        async def scenario() -> None:
            broken = FakePlatform("aiocqhttp", error="bot offline")
            data = FakePlatform("aiocqhttp2", members=[_member(400, nickname="小美")])
            self.service.context = FakeContext([broken, data])

            pending = await self.service._pending_group_members("group:1")
            self.assertEqual([item["user_id"] for item in pending], ["400"])
            self.assertEqual(len(broken.calls), 1)
            self.assertEqual(len(data.calls), 1)

        asyncio.run(scenario())

    def test_reports_when_no_platform_can_answer(self) -> None:
        async def scenario() -> None:
            self.service.context = FakeContext([])
            with self.assertRaises(course_schedule.GroupMemberLookupError):
                await self.service._pending_group_members("group:1")

            self.service.context = FakeContext(
                [FakePlatform("aiocqhttp", error="not in group")]
            )
            with self.assertRaises(course_schedule.GroupMemberLookupError) as caught:
                await self.service._pending_group_members("group:1")
            self.assertIn("not in group", str(caught.exception))

        asyncio.run(scenario())

    def test_works_without_a_context(self) -> None:
        async def scenario() -> None:
            service = course_schedule.CourseScheduleBase.__new__(
                course_schedule.CourseScheduleBase
            )
            service._schedule_store = self.service._schedule_store
            with self.assertRaises(course_schedule.GroupMemberLookupError):
                await service._pending_group_members("group:1")

        asyncio.run(scenario())

    def test_rejects_private_scopes(self) -> None:
        async def scenario() -> None:
            with self.assertRaises(ValueError):
                await self.service._pending_group_members("private:100")
            with self.assertRaises(ValueError):
                await self.service._create_member_schedules("private:100", [])

        asyncio.run(scenario())


class CreateMemberSchedulesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.service = course_schedule.CourseScheduleBase.__new__(
            course_schedule.CourseScheduleBase
        )
        self.service._schedule_store = sqlite_store.SQLiteScheduleStore(
            db_path=Path(self.temp_dir.name) / "schedule.sqlite3"
        )
        self.service.context = FakeContext([])

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_creates_empty_editable_schedules(self) -> None:
        async def scenario() -> None:
            created = await self.service._create_member_schedules(
                "group:1",
                [
                    {"user_id": "100", "name": "小明"},
                    {"user_id": "200", "name": ""},
                ],
                actor="admin",
            )
            self.assertEqual(
                created, [{"user_id": "100", "name": "小明"}, {"user_id": "200", "name": "200"}]
            )

            members = await self.service._schedule_store.get_scope_members("group:1")
            self.assertEqual(set(members), {"100", "200"})
            self.assertEqual(members["100"]["event_count"], 0)
            self.assertEqual(members["100"]["events"], [])
            self.assertEqual(members["100"]["source"], "manual")
            self.assertTrue(members["100"]["ics"].startswith("BEGIN:VCALENDAR"))

            # The WebUI needs the new member to be listed and editable.
            scopes = await self.service._page_scopes()
            scope = scopes["scopes"][0]
            self.assertEqual(scope["scope_id"], "group:1")
            self.assertEqual(scope["member_count"], 2)
            schedule = await self.service._page_schedule("group:1", "100")
            self.assertEqual(schedule["revision"], 1)
            self.assertEqual(schedule["events"], [])
            self.assertEqual(schedule["name"], "小明")

            saved = await self.service._save_page_schedule(
                {
                    "scope_id": "group:1",
                    "user_id": "100",
                    "revision": schedule["revision"],
                    "name": "小明",
                    "events": [
                        {
                            "course": "数学",
                            "start": "2026-10-08T09:00",
                            "end": "2026-10-08T10:30",
                        }
                    ],
                },
                actor="webui",
            )
            self.assertEqual(saved["event_count"], 1)

        asyncio.run(scenario())

    def test_skips_members_that_already_have_a_schedule(self) -> None:
        async def scenario() -> None:
            await self.service._create_member_schedules(
                "group:1", [{"user_id": "100", "name": "小明"}]
            )
            created = await self.service._create_member_schedules(
                "group:1",
                [
                    {"user_id": "100", "name": "小明"},
                    {"user_id": "200", "name": "小红"},
                ],
            )
            self.assertEqual([item["user_id"] for item in created], ["200"])

            with self.assertRaises(ValueError) as caught:
                await self.service._create_member_schedules(
                    "group:1", [{"user_id": "100", "name": "小明"}]
                )
            self.assertIn("都已经有课表", str(caught.exception))

        asyncio.run(scenario())

    def test_rejects_bad_selections(self) -> None:
        async def scenario() -> None:
            with self.assertRaises(ValueError):
                await self.service._create_member_schedules("group:1", [])
            with self.assertRaises(ValueError) as caught:
                await self.service._create_member_schedules(
                    "group:1", [{"user_id": "abc", "name": "谁"}]
                )
            self.assertIn("QQ 号无效", str(caught.exception))
            with self.assertRaises(ValueError):
                await self.service._create_member_schedules(
                    "group:1", [{"user_id": "100", "name": "x" * 201}]
                )
            with self.assertRaises(ValueError):
                await self.service._create_member_schedules(
                    "group:1",
                    [{"user_id": str(index), "name": "x"} for index in range(500)],
                )

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
