from __future__ import annotations

import unittest
from datetime import date, timedelta

from _plugin_loader import load_plugin_module

PACKAGE = "course_schedule_day_view_test_plugin"
day_off = load_plugin_module(PACKAGE, "day_off")
render = load_plugin_module(PACKAGE, "render")
texts = load_plugin_module(PACKAGE, "texts")

TODAY = date(2026, 9, 16)


class DayViewArgumentTests(unittest.TestCase):
    """`/课表 <日期>` takes the same day vocabulary as 休假 and 调休."""

    def test_accepts_every_advertised_form(self) -> None:
        expected = {
            "2026-09-17": date(2026, 9, 17),
            "2026/9/17": date(2026, 9, 17),
            "2026.9.17": date(2026, 9, 17),
            "2026年9月17日": date(2026, 9, 17),
            "9.17": date(2026, 9, 17),
            "9/17": date(2026, 9, 17),
            "9-17": date(2026, 9, 17),
            "9月17日": date(2026, 9, 17),
            "今天": TODAY,
            "明天": date(2026, 9, 17),
            "后天": date(2026, 9, 18),
            "大后天": date(2026, 9, 19),
            "昨天": date(2026, 9, 15),
            "前天": date(2026, 9, 14),
        }
        for text, expected_day in expected.items():
            self.assertEqual(day_off.single_day_query(text, TODAY), (expected_day, ""), text)

    def test_a_yearless_day_follows_the_closest_occurrence(self) -> None:
        # Marking a day always points forward, but viewing one may look back:
        # 9.1 is the start of this month, not of the next year.
        self.assertEqual(day_off.parse_day_token("9.1", TODAY), date(2027, 9, 1))
        self.assertEqual(day_off.single_day_query("9.1", TODAY), (date(2026, 9, 1), ""))
        self.assertEqual(day_off.single_day_query("9.17", TODAY), (date(2026, 9, 17), ""))
        self.assertEqual(day_off.single_day_query("1.3", TODAY), (date(2027, 1, 3), ""))
        self.assertEqual(
            day_off.parse_day_token("12.25", date(2026, 1, 3), roll="nearest"),
            date(2025, 12, 25),
        )

    def test_a_leap_day_without_a_year_finds_a_real_one(self) -> None:
        self.assertEqual(day_off.single_day_query("2.29", TODAY), (date(2028, 2, 29), ""))
        # Neither 2025 nor 2026 has a 29 February, so 2024 is the closest.
        self.assertEqual(
            day_off.single_day_query("2.29", date(2025, 6, 1)), (date(2024, 2, 29), "")
        )

    def test_a_glued_particle_does_not_hide_the_day(self) -> None:
        self.assertEqual(day_off.single_day_query("明天的课", TODAY), (date(2026, 9, 17), ""))
        self.assertEqual(day_off.single_day_query("明天 的课", TODAY), (date(2026, 9, 17), ""))

    def test_no_argument_means_today(self) -> None:
        self.assertEqual(day_off.single_day_query("", TODAY), (None, ""))
        self.assertEqual(day_off.single_day_query("   ", TODAY), (None, ""))

    def test_rejects_an_impossible_day(self) -> None:
        for text in ("2.30", "13月1日", "2026-02-30"):
            day, message = day_off.single_day_query(text, TODAY)
            self.assertIsNone(day)
            self.assertIn("不存在", message)

    def test_rejects_a_range_or_a_list_of_days(self) -> None:
        for text in ("9.17..9.19", "9月17日至9月19日", "9.17 9.18"):
            day, message = day_off.single_day_query(text, TODAY)
            self.assertIsNone(day)
            self.assertIn("一次只能查看一天", message)

    def test_rejects_words_that_are_not_dates(self) -> None:
        for text in ("张三", "9.17 张三", "@小明"):
            day, message = day_off.single_day_query(text, TODAY)
            self.assertIsNone(day)
            self.assertIn("无法识别日期", message)
            self.assertIn("9.17", message)

    def test_the_handler_sees_every_word_of_the_argument(self) -> None:
        # AstrBot binds only the first word after the command to the handler's
        # str parameter, so the handler re-reads the raw message tail.
        class FakeMessageEvent:
            def __init__(self, message: str):
                self.message_str = message

            def get_message_str(self) -> str:
                return self.message_str

        event = FakeMessageEvent("/课表 9月17日 的课")
        self.assertEqual(texts._full_command_tail(event, "9月17日"), "9月17日 的课")
        self.assertEqual(
            day_off.single_day_query(
                texts._full_command_tail(event, "9月17日"), TODAY
            ),
            (date(2026, 9, 17), ""),
        )
        # A range stays visible instead of being silently truncated to its
        # first word, so the user is told only one day can be shown.
        ranged = FakeMessageEvent("/课表 2026-09-17 .. 2026-09-19")
        self.assertEqual(
            day_off.single_day_query(
                texts._full_command_tail(ranged, "2026-09-17"), TODAY
            ),
            (None, "一次只能查看一天（当前解析出 3 天），例如 /课表 9.17 或 /课表 明天。"),
        )


class DayViewLabelTests(unittest.TestCase):
    def test_relative_labels_cover_the_supported_days(self) -> None:
        for delta, label in (
            (0, "今天"),
            (1, "明天"),
            (2, "后天"),
            (3, "大后天"),
            (-1, "昨天"),
            (-2, "前天"),
        ):
            self.assertEqual(day_off.relative_day_text(TODAY + timedelta(days=delta), TODAY), label)
        self.assertEqual(day_off.relative_day_text(TODAY + timedelta(days=10), TODAY), "10 天后")
        self.assertEqual(day_off.relative_day_text(TODAY - timedelta(days=10), TODAY), "10 天前")

    def test_footer_separates_a_live_day_from_an_archived_one(self) -> None:
        self.assertEqual(render.schedule_footer(TODAY, TODAY), render.FOOTER_LIVE)
        self.assertEqual(
            render.schedule_footer(TODAY - timedelta(days=1), TODAY), render.FOOTER_ARCHIVE
        )
        self.assertEqual(
            render.schedule_footer(TODAY + timedelta(days=1), TODAY), render.FOOTER_PLANNED
        )


if __name__ == "__main__":
    unittest.main()
