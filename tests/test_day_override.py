from __future__ import annotations

import asyncio
import json
import sqlite3
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

from _plugin_loader import load_plugin_module

PACKAGE = "course_schedule_day_override_test_plugin"
course_schedule = load_plugin_module(PACKAGE, "course_schedule")
day_off = load_plugin_module(PACKAGE, "day_off")
domain = load_plugin_module(PACKAGE, "domain")
occurrences = load_plugin_module(PACKAGE, "occurrences")
sqlite_store = load_plugin_module(PACKAGE, "sqlite_store")
rank = load_plugin_module(PACKAGE, "rank")
LOCAL_TZ = load_plugin_module(PACKAGE, "constants").LOCAL_TZ


def _bounds(start: str, end: str = "") -> tuple[datetime, datetime]:
    start_bound = datetime.fromisoformat(f"{start} 00:00").replace(tzinfo=LOCAL_TZ)
    end_value = end or start
    end_bound = datetime.fromisoformat(f"{end_value} 00:00").replace(tzinfo=LOCAL_TZ)
    return start_bound, end_bound + timedelta(days=1)


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


class DayTokenTests(unittest.TestCase):
    def test_relative_and_absolute_days(self) -> None:
        today = date(2026, 9, 14)
        self.assertEqual(day_off.parse_day_token("今天", today), today)
        self.assertEqual(day_off.parse_day_token("明天", today), date(2026, 9, 15))
        self.assertEqual(day_off.parse_day_token("后天", today), date(2026, 9, 16))
        self.assertEqual(day_off.parse_day_token("昨天", today), date(2026, 9, 13))
        self.assertEqual(day_off.parse_day_token("2026-10-01", today), date(2026, 10, 1))
        self.assertEqual(day_off.parse_day_token("2026/10/1", today), date(2026, 10, 1))
        self.assertEqual(day_off.parse_day_token("10月8日", today), date(2026, 10, 8))
        self.assertIsNone(day_off.parse_day_token("小明", today))

    def test_yearless_day_rolls_forward(self) -> None:
        today = date(2026, 12, 20)
        self.assertEqual(day_off.parse_day_token("1月3日", today), date(2027, 1, 3))

    def test_impossible_day_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            day_off.parse_day_token("2026-02-30", date(2026, 9, 14))

    def test_splits_days_from_the_target_query(self) -> None:
        today = date(2026, 9, 14)
        days, rest = day_off.split_day_override_args(
            "2026-10-11 2026-10-08 小明", today
        )
        self.assertEqual(days, [date(2026, 10, 11), date(2026, 10, 8)])
        self.assertEqual(rest, ["小明"])

    def test_reads_ranges_and_glued_particles(self) -> None:
        today = date(2026, 9, 14)
        days, rest = day_off.split_day_override_args("10月1日至10月8日", today)
        self.assertEqual(len(days), 8)
        self.assertEqual(days[0], date(2026, 10, 1))
        self.assertEqual(days[-1], date(2026, 10, 8))
        self.assertEqual(rest, [])

        days, rest = day_off.split_day_override_args("调休 10-11 上10-08的课 @小红", today)
        self.assertEqual(days, [date(2026, 10, 11), date(2026, 10, 8)])
        self.assertEqual(rest, ["小红"])

    def test_keeps_nicknames_that_start_with_a_particle(self) -> None:
        days, rest = day_off.split_day_override_args("10-01 上官婉儿", date(2026, 9, 14))
        self.assertEqual(days, [date(2026, 10, 1)])
        self.assertEqual(rest, ["上官婉儿"])

    def test_a_spaced_range_still_covers_the_whole_span(self) -> None:
        today = date(2026, 9, 14)
        for text in ("10月1日 .. 10月8日", "10月1日~10月8日", "10月1日 — 10月8日"):
            days, rest = day_off.split_day_override_args(text, today)
            self.assertEqual(len(days), 8, text)
            self.assertEqual(rest, [], text)

        # 调休 passes the two dates as a pair, so a spaced 到 must keep them
        # apart instead of joining them into one range.
        days, rest = day_off.split_day_override_args("10月11日 到 10月8日", today)
        self.assertEqual(days, [date(2026, 10, 11), date(2026, 10, 8)])
        self.assertEqual(rest, [])

        # 10月1日 至 10月8日 is read as the two named days, which for 休假 has
        # the same effect as the range above.
        days, _rest = day_off.split_day_override_args("10月1日 至 10月8日", today)
        self.assertEqual(days, [date(2026, 10, 1), date(2026, 10, 8)])


class DayOverrideExpansionTests(unittest.TestCase):
    def _member(self, **overrides) -> dict[str, object]:
        return {
            "name": "小明",
            "events": [
                domain.make_event("数学", "2026-10-08 09:00", "2026-10-08 10:30"),
                domain.make_event("英语", "2026-10-11 09:00", "2026-10-11 10:00"),
            ],
            "_day_overrides": overrides,
        }

    def test_holiday_drops_every_course_of_that_day(self) -> None:
        member = self._member(
            **{"2026-10-08": {"kind": "holiday", "source_day": ""}}
        )
        found = occurrences._expand_member_occurrences(member, *_bounds("2026-10-08"))
        self.assertEqual(found, [])

        other = occurrences._expand_member_occurrences(member, *_bounds("2026-10-11"))
        self.assertEqual([item["SUMMARY"] for item in other], ["英语"])

    def test_shift_replaces_a_day_with_the_source_day(self) -> None:
        member = self._member(
            **{"2026-10-11": {"kind": "shift", "source_day": "2026-10-08"}}
        )
        found = occurrences._expand_member_occurrences(member, *_bounds("2026-10-11"))
        self.assertEqual([item["SUMMARY"] for item in found], ["数学"])
        start, end = found[0]["_start"], found[0]["_end"]
        self.assertEqual(f"{start:%Y-%m-%d %H:%M}", "2026-10-11 09:00")
        self.assertEqual(f"{end:%H:%M}", "10:30")
        self.assertEqual(found[0]["_shifted_from"], "2026-10-08")

    def test_shift_source_still_works_when_the_source_day_is_a_holiday(self) -> None:
        member = self._member(
            **{
                "2026-10-08": {"kind": "holiday", "source_day": ""},
                "2026-10-11": {"kind": "shift", "source_day": "2026-10-08"},
            }
        )
        holiday = occurrences._expand_member_occurrences(member, *_bounds("2026-10-08"))
        make_up = occurrences._expand_member_occurrences(member, *_bounds("2026-10-11"))
        self.assertEqual(holiday, [])
        self.assertEqual([item["SUMMARY"] for item in make_up], ["数学"])

    def test_weekly_courses_follow_the_source_weekday(self) -> None:
        member = {
            "name": "小明",
            "events": [
                domain.make_event(
                    "周三实验",
                    "2026-09-02 14:00",
                    "2026-09-02 16:00",
                    rrule="FREQ=WEEKLY;BYDAY=WE",
                )
            ],
            "_day_overrides": {"2026-10-11": {"kind": "shift", "source_day": "2026-10-07"}},
        }
        found = occurrences._expand_member_occurrences(member, *_bounds("2026-10-11"))
        self.assertEqual([item["SUMMARY"] for item in found], ["周三实验"])
        self.assertEqual(f"{found[0]['_start']:%H:%M}", "14:00")

    def test_rank_board_skips_cancelled_days(self) -> None:
        member = {
            "name": "小明",
            "events": [
                domain.make_event("数学", "2026-10-08 09:00", "2026-10-08 11:00"),
                domain.make_event("英语", "2026-10-09 09:00", "2026-10-09 10:00"),
            ],
            "_day_overrides": {"2026-10-08": {"kind": "holiday", "source_day": ""}},
        }
        rows = rank.build_rank_rows(
            {"100": member}, *_bounds("2026-10-08", "2026-10-09")
        )
        self.assertEqual(rows[0]["minutes"], 60)
        self.assertEqual(rows[0]["course_count"], 1)

    def test_daily_rows_mark_holiday_and_shift(self) -> None:
        members = {
            "1": self._member(**{"2026-10-08": {"kind": "holiday", "source_day": ""}}),
            "2": {
                "name": "小红",
                "events": [
                    domain.make_event("数学", "2026-10-08 09:00", "2026-10-08 10:30"),
                    domain.make_event("英语", "2026-10-11 09:00", "2026-10-11 10:00"),
                ],
                "_day_overrides": {
                    "2026-10-08": {"kind": "shift", "source_day": "2026-10-11"}
                },
            },
        }
        rows = {
            row["user_id"]: row
            for row in domain.daily_member_rows(
                members, date(2026, 10, 8), now=datetime(2026, 10, 8, 8, 0, tzinfo=LOCAL_TZ)
            )
        }
        self.assertEqual(rows["1"]["status_key"], "holiday")
        self.assertEqual(rows["1"]["course"], "休假 · 无课程安排")
        self.assertEqual(rows["1"]["countdown"], "当天课程全部取消")
        self.assertEqual(rows["1"]["override_kind"], "holiday")

        self.assertEqual(rows["2"]["status_key"], "upcoming")
        self.assertEqual(rows["2"]["course"], "英语")
        self.assertEqual(rows["2"]["override_kind"], "shift")
        self.assertEqual(rows["2"]["override_source"], "2026-10-11")
        self.assertIn("调休 · 按 10-11 的课表", rows["2"]["time"])


class DayOverrideStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "course_schedule.sqlite3"

    async def asyncTearDown(self) -> None:
        self.temp_dir.cleanup()

    async def test_markers_are_scoped_and_deletable(self) -> None:
        store = sqlite_store.SQLiteScheduleStore(db_path=self.db_path)
        await store.set_day_override(
            "group:1", "*", "2026-10-01", "holiday", created_by="100"
        )
        await store.set_day_override(
            "group:1",
            "200",
            "2026-10-11",
            "shift",
            source_day="2026-10-08",
            created_by="100",
        )
        await store.set_day_override("group:2", "*", "2026-10-01", "holiday")

        rows = await store.list_day_overrides("group:1")
        self.assertEqual(
            [(row["day"], row["kind"], row["user_id"]) for row in rows],
            [("2026-10-01", "holiday", "*"), ("2026-10-11", "shift", "200")],
        )
        self.assertEqual(len(await store.list_day_overrides("group:2")), 1)

        self.assertTrue(await store.delete_day_override("group:1", "*", "2026-10-01"))
        self.assertFalse(await store.delete_day_override("group:1", "*", "2026-10-01"))
        self.assertEqual(len(await store.list_day_overrides("group:1")), 1)

    async def test_member_rules_win_over_scope_wide_rules(self) -> None:
        store = sqlite_store.SQLiteScheduleStore(db_path=self.db_path)
        await store.put_member("group:1", "100", {"name": "小明"})
        await store.set_day_override("group:1", "*", "2026-10-01", "holiday")
        await store.set_day_override(
            "group:1", "100", "2026-10-01", "shift", source_day="2026-10-08"
        )

        members = await store.get_scope_members("group:1")
        self.assertEqual(
            members["100"]["_day_overrides"]["2026-10-01"],
            {"kind": "shift", "source_day": "2026-10-08", "created_by": "", "created_at": ""},
        )
        # A member that appears later still inherits the scope-wide marker.
        await store.put_member("group:1", "300", {"name": "小刚"})
        later = await store.get_member("group:1", "300")
        self.assertEqual(later["_day_overrides"]["2026-10-01"]["kind"], "holiday")

    async def test_markers_are_not_written_into_member_data(self) -> None:
        store = sqlite_store.SQLiteScheduleStore(db_path=self.db_path)
        await store.set_day_override("group:1", "100", "2026-10-01", "holiday")
        member = await store.get_member("group:1", "100")
        await store.put_member("group:1", "100", member or {"name": "小明"})

        with sqlite3.connect(self.db_path) as conn:
            payload = conn.execute(
                "SELECT data_json FROM schedule_members WHERE scope_id='group:1' AND user_id='100'"
            ).fetchone()[0]
        self.assertNotIn("_day_overrides", payload)
        self.assertNotIn("_revision", payload)
        stored = json.loads(payload)
        self.assertEqual(stored["name"], "小明")


class DayOverrideCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        base = course_schedule.CourseScheduleBase.__new__(
            course_schedule.CourseScheduleBase
        )
        base._schedule_store = sqlite_store.SQLiteScheduleStore(
            db_path=Path(self.temp_dir.name) / "schedule.sqlite3"
        )
        self.service = base
        self.member = FakeEvent("100")
        self.admin = FakeEvent("100", admin=True)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _seed(self) -> None:
        async def scenario() -> None:
            await self.service._edit_schedule_text(
                self.member,
                "create",
                course="数学",
                start_time="2026-10-08 09:00",
                end_time="2026-10-08 10:30",
            )
            await self.service._edit_schedule_text(
                self.admin,
                "create",
                person="200",
                member_name="小红",
                course="英语",
                start_time="2026-10-11 09:00",
                end_time="2026-10-11 10:00",
            )

        asyncio.run(scenario())

    def test_admin_defaults_to_the_whole_group(self) -> None:
        self._seed()

        async def scenario() -> None:
            result = await self.service._day_override_command_text(
                self.admin, "2026-10-01", "holiday"
            )
            self.assertIn("已将 2026-10-01 标记为休假（全体成员）", result)

            members = await self.service._get_scope_members(self.member)
            self.assertEqual(members["100"]["_day_overrides"]["2026-10-01"]["kind"], "holiday")
            self.assertEqual(members["200"]["_day_overrides"]["2026-10-01"]["kind"], "holiday")
            stored = await self.service._schedule_store.list_day_overrides("group:1")
            self.assertEqual([row["user_id"] for row in stored], ["*"])

        asyncio.run(scenario())

    def test_member_can_only_mark_themselves(self) -> None:
        self._seed()

        async def scenario() -> None:
            own = await self.service._day_override_command_text(
                self.member, "2026-10-01", "holiday"
            )
            self.assertIn("已将 2026-10-01 标记为休假（小明(100)）", own)

            denied = await self.service._day_override_command_text(
                self.member, "2026-10-01 小红", "holiday"
            )
            self.assertIn("需要管理员权限", denied)

            denied_all = await self.service._day_override_command_text(
                self.member, "2026-10-01 全部", "holiday"
            )
            self.assertIn("只有管理员", denied_all)

        asyncio.run(scenario())

    def test_admin_can_target_one_member_by_nickname_or_mention(self) -> None:
        self._seed()

        async def scenario() -> None:
            by_name = await self.service._day_override_command_text(
                self.admin, "2026-10-02 小红", "holiday"
            )
            self.assertIn("已将 2026-10-02 标记为休假（小红(200)）", by_name)

            mention_event = FakeEvent(
                "100", admin=True, messages=[FakeAt("bot"), FakeAt("300", "小麻瓜")]
            )
            by_mention = await self.service._day_override_command_text(
                mention_event, "2026-10-03 @小麻瓜", "holiday"
            )
            self.assertIn("（300）", by_mention)

            members = await self.service._get_scope_members(self.member)
            self.assertNotIn("2026-10-02", members["100"]["_day_overrides"])
            self.assertEqual(
                members["200"]["_day_overrides"]["2026-10-02"]["kind"], "holiday"
            )

        asyncio.run(scenario())

    def test_holiday_and_make_up_day_change_what_the_tools_report(self) -> None:
        self._seed()

        async def scenario() -> None:
            await self.service._day_override_command_text(
                self.admin, "2026-10-08", "holiday"
            )
            cancelled = await self.service._find_schedule_text(
                self.member, time_range="2026-10-08"
            )
            self.assertIn("没有找到符合条件的课程", cancelled)

            make_up = await self.service._day_override_command_text(
                self.admin, "2026-10-11 2026-10-08", "shift"
            )
            self.assertIn("已将 2026-10-11 标记为调休（全体成员）", make_up)
            self.assertIn("改为 2026-10-08 的课程", make_up)

            moved = await self.service._find_schedule_text(
                self.member, time_range="2026-10-11"
            )
            self.assertIn("数学", moved)
            self.assertIn("调休自 2026-10-08", moved)

            # The make-up day replaces 10-11 with 10-08's timetable, so a member
            # without a 10-08 course has nothing to attend that day.
            others = await self.service._find_schedule_text(
                self.member, person="小红", time_range="2026-10-11"
            )
            self.assertIn("没有找到符合条件的课程", others)

        asyncio.run(scenario())

    def test_member_marker_wins_over_a_group_wide_marker(self) -> None:
        self._seed()

        async def scenario() -> None:
            await self.service._day_override_command_text(
                self.admin, "2026-10-11", "holiday"
            )
            await self.service._day_override_command_text(
                self.admin, "2026-10-11 2026-10-08 小红", "shift"
            )
            members = await self.service._get_scope_members(self.member)
            self.assertEqual(
                members["200"]["_day_overrides"]["2026-10-11"]["kind"], "shift"
            )
            self.assertEqual(
                members["100"]["_day_overrides"]["2026-10-11"]["kind"], "holiday"
            )

        asyncio.run(scenario())

    def test_command_parsing_and_error_messages(self) -> None:
        async def scenario() -> None:
            missing = await self.service._day_override_command_text(
                self.admin, "", "holiday"
            )
            self.assertIn("请提供日期", missing)

            no_source = await self.service._day_override_command_text(
                self.admin, "2026-10-11", "shift"
            )
            self.assertIn("调休需要来源日期", no_source)

            same_day = await self.service._day_override_command_text(
                self.admin, "2026-10-11 2026-10-11", "shift"
            )
            self.assertIn("不能和调休日期相同", same_day)

            far_away = await self.service._day_override_command_text(
                self.admin, "2026-10-11 2030-10-08", "shift"
            )
            self.assertIn("相差不能超过", far_away)

            unknown = await self.service._day_override_command_text(
                self.admin, "2026-10-11 2026-10-08 查无此人", "shift"
            )
            self.assertIn("没有找到成员", unknown)

        asyncio.run(scenario())

    def test_holiday_range_and_listing_then_cancel(self) -> None:
        self._seed()

        async def scenario() -> None:
            marked = await self.service._day_override_command_text(
                self.admin, "10月1日至10月8日", "holiday"
            )
            self.assertIn("已将 2026-10-01 至 2026-10-08 标记为休假（全体成员，共 8 天）", marked)

            listed = await self.service._day_override_list_text(self.member)
            self.assertIn("共 8 条", listed)
            self.assertIn("2026-10-01 休假：当天课程全部取消（全体成员）", listed)

            cancelled = await self.service._day_override_clear_command_text(
                self.admin, "10月1日至10月8日"
            )
            self.assertIn(
                "已取消 2026-10-01 至 2026-10-08 的休假标记（全体成员，共 8 天）", cancelled
            )
            self.assertEqual(
                await self.service._schedule_store.list_day_overrides("group:1"), []
            )
            self.assertIn(
                "没有可取消", await self.service._day_override_clear_command_text(
                    self.admin, "10月1日"
                )
            )

        asyncio.run(scenario())

    def test_member_cannot_cancel_a_group_wide_marker(self) -> None:
        self._seed()

        async def scenario() -> None:
            await self.service._day_override_command_text(
                self.admin, "2026-10-01", "holiday"
            )
            blocked = await self.service._day_override_clear_command_text(
                self.member, "2026-10-01"
            )
            self.assertIn("请让管理员使用 /销假 取消", blocked)
            self.assertEqual(
                len(await self.service._schedule_store.list_day_overrides("group:1")), 1
            )

            own = await self.service._day_override_command_text(
                self.member, "2026-10-02", "holiday"
            )
            self.assertIn("小明(100)", own)
            removed = await self.service._day_override_clear_command_text(
                self.member, "2026-10-02"
            )
            self.assertIn("已取消 2026-10-02 的休假标记（小明(100)）", removed)

        asyncio.run(scenario())

    def test_private_chat_only_touches_the_sender(self) -> None:
        self._seed()

        async def scenario() -> None:
            private = FakeEvent("100", group_id="")
            await self.service._edit_schedule_text(
                private,
                "create",
                course="数学",
                start_time="2026-10-08 09:00",
                end_time="2026-10-08 10:30",
            )
            result = await self.service._day_override_command_text(
                private, "2026-10-01 小红", "holiday"
            )
            self.assertIn("私聊只能标记自己的假期", result)

            own = await self.service._day_override_command_text(
                private, "2026-10-01", "holiday"
            )
            self.assertIn("已将 2026-10-01 标记为休假（小明(100)）", own)
            rows = await self.service._schedule_store.list_day_overrides("private:100")
            self.assertEqual([row["user_id"] for row in rows], ["100"])

        asyncio.run(scenario())

    def test_day_image_reports_the_marked_day(self) -> None:
        self._seed()

        async def scenario() -> None:
            await self.service._day_override_command_text(
                self.admin, "2026-10-08", "holiday"
            )
            await self.service._day_override_command_text(
                self.admin, "2026-10-11 2026-10-08", "shift"
            )
            holiday_rows = domain.daily_member_rows(
                await self.service._get_scope_members(self.member),
                date(2026, 10, 8),
                now=datetime(2026, 10, 8, 8, 0, tzinfo=LOCAL_TZ),
            )
            self.assertTrue(all(row["status_key"] == "holiday" for row in holiday_rows))

            make_up_rows = domain.daily_member_rows(
                await self.service._get_scope_members(self.member),
                date(2026, 10, 11),
                now=datetime(2026, 10, 11, 8, 0, tzinfo=LOCAL_TZ),
            )
            mine = next(row for row in make_up_rows if row["user_id"] == "100")
            self.assertEqual(mine["course"], "数学")
            self.assertIn("调休 · 按 10-08 的课表", mine["time"])

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
