from __future__ import annotations

import unittest
from datetime import date, datetime

from _plugin_loader import load_plugin_module

PACKAGE = "course_schedule_daily_test_plugin"
domain = load_plugin_module(PACKAGE, "domain")
LOCAL_TZ = load_plugin_module(PACKAGE, "constants").LOCAL_TZ


def _occurrence(hour: int, end_hour: int, course: str) -> dict[str, object]:
    return domain.make_event(
        course,
        f"2026-08-31 {hour:02d}:00",
        f"2026-08-31 {end_hour:02d}:00",
    )


class DailyScheduleTests(unittest.TestCase):
    def test_builds_one_sorted_status_row_per_member(self) -> None:
        members = {
            "1": {"name": "&#x1F600;小明", "events": [_occurrence(9, 10, "数学")]},
            "2": {"name": "小红👩‍🏫", "events": [_occurrence(14, 15, "英语")]},
            "3": {"name": "小刚", "events": [_occurrence(7, 8, "体育")]},
            "4": {"name": "休息日", "events": []},
        }
        rows = domain.daily_member_rows(
            members,
            date(2026, 8, 31),
            now=datetime(2026, 8, 31, 9, 30, tzinfo=LOCAL_TZ),
        )

        self.assertEqual(
            [row["status_key"] for row in rows],
            ["active", "upcoming", "finished", "none"],
        )
        self.assertEqual(rows[0]["course"], "数学")
        self.assertEqual(rows[0]["name"], "😀小明")
        self.assertEqual(rows[0]["duration"], "1小时")
        self.assertEqual(rows[0]["countdown"], "30分钟")
        self.assertEqual(rows[1]["countdown"], "4小时30分钟")
        self.assertEqual(rows[2]["countdown"], "今天的课程都上完啦")

    def test_another_day_is_not_described_as_today(self) -> None:
        members = {"1": {"name": "小明", "events": []}}
        rows = domain.daily_member_rows(
            members,
            date(2026, 8, 30),
            now=datetime(2026, 8, 31, 9, 30, tzinfo=LOCAL_TZ),
        )
        self.assertEqual(rows[0]["status"], "当天无课")
        self.assertEqual(rows[0]["time"], "当天没有安排课程")
        self.assertEqual(rows[0]["countdown_label"], "课程状态")


if __name__ == "__main__":
    unittest.main()
