from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from _plugin_loader import load_plugin_module

course_schedule = load_plugin_module("course_schedule_agent_test_plugin", "course_schedule")
texts = load_plugin_module("course_schedule_agent_test_plugin", "texts")


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
        self_id: str = "bot",
    ):
        self.sender_id = sender_id
        self.group_id = group_id
        self.admin = admin
        self.messages = messages or []
        self.self_id = self_id

    def get_sender_id(self) -> str:
        return self.sender_id

    def get_sender_name(self) -> str:
        return {"100": "小明", "200": "小红"}.get(self.sender_id, self.sender_id)

    def get_group_id(self) -> str:
        return self.group_id

    def get_messages(self) -> list[object]:
        return self.messages

    def get_self_id(self) -> str:
        return self.self_id

    def is_admin(self) -> bool:
        return self.admin


class AgentToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        # Bypass the service constructor's AstrBot data-directory lookup; the
        # test supplies its own isolated SQLite store below.
        base = course_schedule.CourseScheduleBase.__new__(
            course_schedule.CourseScheduleBase
        )
        base._schedule_store = course_schedule.SQLiteScheduleStore(
            db_path=Path(self.temp_dir.name) / "schedule.sqlite3"
        )
        self.service = base
        self.event = FakeEvent("100")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_edit_create_find_update_delete_and_clear(self) -> None:
        async def scenario() -> None:
            created = await self.service._edit_schedule_text(
                self.event,
                "create",
                course="数学",
                start_time="2026-09-01 09:00",
                end_time="2026-09-01 10:30",
                location="A101",
                description="带练习",
            )
            self.assertIn("已新增", created)

            found = await self.service._find_schedule_text(
                self.event, person="小明", field="课程", value="数学"
            )
            self.assertIn("course_id=1", found)
            self.assertIn("时长：90分钟", found)

            updated = await self.service._edit_schedule_text(
                self.event,
                "修改",
                course_id=1,
                course="高等数学",
                clear_fields="地点,备注",
            )
            self.assertIn("已更新", updated)
            info = await self.service._schedule_store.get_member("group:1", "100")
            self.assertEqual(info["events"][0]["SUMMARY"], "高等数学")
            self.assertNotIn("LOCATION", info["events"][0])
            self.assertNotIn("DESCRIPTION", info["events"][0])

            deleted = await self.service._edit_schedule_text(
                self.event, "delete", course_id=1
            )
            self.assertIn("已删除", deleted)
            info = await self.service._schedule_store.get_member("group:1", "100")
            self.assertEqual(info["events"], [])

        asyncio.run(scenario())

    def test_find_expands_recurrence_and_supports_all_members(self) -> None:
        async def scenario() -> None:
            await self.service._edit_schedule_text(
                self.event,
                "add",
                course="英语",
                start_time="2026-09-01 09:00",
                end_time="2026-09-01 10:00",
                rrule="FREQ=WEEKLY;BYDAY=TU",
            )
            admin_event = FakeEvent("100", admin=True)
            await self.service._edit_schedule_text(
                admin_event,
                "add",
                person="200",
                member_name="小红",
                course="物理",
                start_time="2026-09-02 11:00",
                end_time="2026-09-02 12:00",
            )

            next_week = await self.service._find_schedule_text(
                self.event, time_range="2026-09-08"
            )
            self.assertIn("英语", next_week)
            self.assertNotIn("物理", next_week)

            own_member = await self.service._find_schedule_text(self.event)
            self.assertIn("小明(100)", own_member)
            self.assertNotIn("小红(200)", own_member)

            all_members = await self.service._find_schedule_text(
                self.event, person="all"
            )
            self.assertIn("成员列表（2 人）", all_members)
            self.assertIn("小明(100)", all_members)
            self.assertIn("小红(200)", all_members)

        asyncio.run(scenario())

    def test_only_admin_can_edit_another_group_member(self) -> None:
        async def scenario() -> None:
            admin_event = FakeEvent("100", admin=True)
            await self.service._edit_schedule_text(
                admin_event,
                "create",
                person="200",
                course="物理",
                start_time="2026-09-01 11:00",
                end_time="2026-09-01 12:00",
            )
            denied = await self.service._edit_schedule_text(
                self.event,
                "update",
                person="200",
                course_id=1,
                course="化学",
            )
            self.assertIn("需要管理员权限", denied)

            allowed = await self.service._edit_schedule_text(
                admin_event,
                "update",
                person="200",
                course_id=1,
                course="化学",
            )
            self.assertIn("已更新", allowed)
            info = await self.service._schedule_store.get_member("group:1", "200")
            self.assertEqual(info["events"][0]["SUMMARY"], "化学")

        asyncio.run(scenario())

    def test_admin_can_create_unknown_nickname_from_message_mention(self) -> None:
        async def scenario() -> None:
            admin_event = FakeEvent(
                "100",
                admin=True,
                messages=[FakeAt("bot"), FakeAt("200", "小麻瓜")],
            )
            result = await self.service._edit_schedule_text(
                admin_event,
                "create",
                person="小麻瓜",
                member_name="小麻瓜",
                course="复变函数",
                start_time="2026-09-01 08:00",
                end_time="2026-09-01 09:45",
            )
            self.assertIn("已新增", result)
            info = await self.service._schedule_store.get_member("group:1", "200")
            self.assertEqual(info["name"], "小麻瓜")
            self.assertEqual(info["events"][0]["SUMMARY"], "复变函数")

        asyncio.run(scenario())


    def test_rank_board_orders_members_by_hours(self) -> None:
        async def scenario() -> None:
            await self.service._edit_schedule_text(
                self.event,
                "create",
                course="数学",
                start_time="2026-09-01 09:00",
                end_time="2026-09-01 11:00",
            )
            admin_event = FakeEvent("100", admin=True)
            await self.service._edit_schedule_text(
                admin_event,
                "create",
                person="200",
                member_name="小红",
                course="英语",
                start_time="2026-09-01 09:00",
                end_time="2026-09-01 10:00",
            )

            rows, label = await self.service._rank_board_rows(
                self.event, "2026-09-01..2026-09-01"
            )
            self.assertEqual(label, "2026-09-01")
            self.assertEqual([row["name"] for row in rows], ["小明", "小红"])
            self.assertEqual([row["rank"] for row in rows], [1, 2])
            self.assertEqual(rows[0]["hours_text"], "2小时")
            self.assertEqual(rows[0]["progress"], 1.0)
            self.assertEqual(rows[1]["progress"], 0.5)

            # The default period is the current week, so a fixed past date window
            # that excludes September would report nothing.
            empty, _label = await self.service._rank_board_rows(
                self.event, "2026-01-01..2026-01-07"
            )
            self.assertEqual(empty, [])

        asyncio.run(scenario())

    def test_rank_board_rejects_bad_and_overlong_periods(self) -> None:
        async def scenario() -> None:
            await self.service._edit_schedule_text(
                self.event,
                "create",
                course="数学",
                start_time="2026-09-01 09:00",
                end_time="2026-09-01 10:00",
            )
            with self.assertRaises(ValueError):
                await self.service._rank_board_rows(self.event, "昨天下午")
            with self.assertRaises(ValueError):
                await self.service._rank_board_rows(self.event, "2020-01-01..2030-01-01")

        asyncio.run(scenario())

    def test_find_accepts_a_full_datetime_range(self) -> None:
        async def scenario() -> None:
            await self.service._edit_schedule_text(
                self.event,
                "create",
                course="数学",
                start_time="2026-09-01 09:00",
                end_time="2026-09-01 10:30",
            )
            inside = await self.service._find_schedule_text(
                self.event, time_range="2026-09-01 08:00..2026-09-01 12:00"
            )
            self.assertIn("数学", inside)

            outside = await self.service._find_schedule_text(
                self.event, time_range="2026-09-01 11:00..2026-09-01 12:00"
            )
            self.assertIn("没有找到符合条件的课程", outside)

        asyncio.run(scenario())

    def test_find_reports_a_bad_filter_value_even_when_nothing_matches(self) -> None:
        async def scenario() -> None:
            await self.service._edit_schedule_text(
                self.event,
                "create",
                course="数学",
                start_time="2026-09-01 09:00",
                end_time="2026-09-01 10:30",
            )
            # The value is unusable, so the error must not be reported as an
            # empty result just because no row happened to match.
            bad_duration = await self.service._find_schedule_text(
                self.event, field="duration", value="一小时"
            )
            self.assertIn("duration value 应为分钟数", bad_duration)

            bad_weekday = await self.service._find_schedule_text(
                self.event, field="weekday", value="周八"
            )
            self.assertIn("weekday value", bad_weekday)

            bad_date = await self.service._find_schedule_text(
                self.event, field="date", value="9月1日"
            )
            self.assertIn("date value 应使用 YYYY-MM-DD", bad_date)

        asyncio.run(scenario())

    def test_find_filters_by_status_alias_and_duration(self) -> None:
        async def scenario() -> None:
            await self.service._edit_schedule_text(
                self.event,
                "create",
                course="数学",
                start_time="2026-09-01 09:00",
                end_time="2026-09-01 10:30",
            )
            past = await self.service._find_schedule_text(
                self.event, field="status", value="已结束"
            )
            self.assertIn("数学", past)
            self.assertIn("状态：past", past)

            matching = await self.service._find_schedule_text(
                self.event, field="duration", value="90"
            )
            self.assertIn("数学", matching)

            missing = await self.service._find_schedule_text(
                self.event, field="duration", value="45"
            )
            self.assertIn("没有找到符合条件的课程", missing)

        asyncio.run(scenario())

    def test_command_tail_reads_past_the_bound_argument(self) -> None:
        event = FakeEvent("100")
        event.message_str = "/上课时长榜 2026-09-01 .. 2026-09-30"
        # AstrBot binds only the first word to a plain str parameter; the rest of
        # the range has to come from the raw message.
        self.assertEqual(texts._command_tail(event, "2026-09-01"), "2026-09-01")
        self.assertEqual(texts._command_tail(event, ""), "2026-09-01 .. 2026-09-30")
        self.assertEqual(texts._command_tail(event, "  本周  "), "本周")
        # No message text at all (and a bare command) both yield an empty tail.
        self.assertEqual(texts._command_tail(FakeEvent("100"), ""), "")
        event.message_str = "/上课时长榜"
        self.assertEqual(texts._command_tail(event, ""), "")

    def test_full_command_tail_keeps_multi_argument_commands_intact(self) -> None:
        event = FakeEvent("100")
        event.message_str = "/调休 2026-10-11 2026-10-08 @小明"
        # The bound parameter only carries the first word, so 调休 would lose its
        # source date without reading the rest of the message.
        self.assertEqual(
            texts._full_command_tail(event, "2026-10-11"),
            "2026-10-11 2026-10-08 @小明",
        )
        # Adapters that bind the whole remainder are left untouched.
        self.assertEqual(
            texts._full_command_tail(event, "2026-10-11 2026-10-08 @小明"),
            "2026-10-11 2026-10-08 @小明",
        )
        # A rewritten bound value that the raw tail does not continue still wins.
        self.assertEqual(texts._full_command_tail(event, "2026-10-08"), "2026-10-08")
        self.assertEqual(texts._full_command_tail(event, ""), "2026-10-11 2026-10-08 @小明")
        self.assertEqual(texts._full_command_tail(FakeEvent("100"), ""), "")


if __name__ == "__main__":
    unittest.main()
