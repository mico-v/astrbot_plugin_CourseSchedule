from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta

from _plugin_loader import load_plugin_module

PACKAGE = "course_schedule_rank_test_plugin"
rank = load_plugin_module(PACKAGE, "rank")
domain = load_plugin_module(PACKAGE, "domain")
time_range = load_plugin_module(PACKAGE, "time_range")
LOCAL_TZ = load_plugin_module(PACKAGE, "constants").LOCAL_TZ


def _bounds(start: str, end: str) -> tuple[datetime, datetime]:
    """Full-day window covering ``start..end`` inclusive."""
    start_bound = datetime.fromisoformat(f"{start} 00:00").replace(tzinfo=LOCAL_TZ)
    end_bound = datetime.fromisoformat(f"{end} 00:00").replace(tzinfo=LOCAL_TZ)
    return start_bound, end_bound + timedelta(days=1)


def _event(course: str, start: str, end: str, **kwargs) -> dict[str, str]:
    return domain.make_event(course, start, end, **kwargs)


class RankBoardTests(unittest.TestCase):
    def test_union_metric_counts_overlapping_courses_once(self) -> None:
        members = {
            "1": {
                "name": "小明",
                "events": [
                    _event("数学", "2026-09-01 09:00", "2026-09-01 11:00"),
                    _event("英语", "2026-09-01 10:00", "2026-09-01 12:00"),
                ],
            }
        }
        start, end = _bounds("2026-09-01", "2026-09-01")

        union = rank.build_rank_rows(members, start, end)
        summed = rank.build_rank_rows(members, start, end, metric="sum")

        self.assertEqual(union[0]["minutes"], 180)
        self.assertEqual(union[0]["hours_text"], "3小时")
        self.assertEqual(union[0]["course_count"], 2)
        self.assertEqual(summed[0]["minutes"], 240)

    def test_clips_occurrences_to_the_window(self) -> None:
        members = {
            "1": {
                "name": "小明",
                "events": [_event("晚自习", "2026-09-01 23:00", "2026-09-02 01:00")],
            }
        }
        rows = rank.build_rank_rows(members, *_bounds("2026-09-01", "2026-09-01"))
        self.assertEqual(rows[0]["minutes"], 60)

    def test_weekly_recurrence_counts_once_per_week(self) -> None:
        members = {
            "1": {
                "name": "小明",
                "events": [
                    _event(
                        "英语",
                        "2026-09-01 09:00",
                        "2026-09-01 10:00",
                        rrule="FREQ=WEEKLY;BYDAY=TU",
                    )
                ],
            }
        }

        one_week = rank.build_rank_rows(members, *_bounds("2026-09-01", "2026-09-07"))
        two_weeks = rank.build_rank_rows(members, *_bounds("2026-09-01", "2026-09-14"))

        self.assertEqual((one_week[0]["minutes"], one_week[0]["course_count"]), (60, 1))
        self.assertEqual((two_weeks[0]["minutes"], two_weeks[0]["course_count"]), (120, 2))

    def test_all_day_events_are_excluded_but_reported(self) -> None:
        all_day = {
            "UID": "holiday",
            "SUMMARY": "校运会",
            "DTSTART": "20260901",
            "DTEND": "20260902",
        }
        members = {
            "1": {
                "name": "小明",
                "events": [all_day, _event("数学", "2026-09-01 09:00", "2026-09-01 10:00")],
            }
        }
        start, end = _bounds("2026-09-01", "2026-09-01")

        rows = rank.build_rank_rows(members, start, end)
        self.assertEqual(rows[0]["minutes"], 60)
        self.assertEqual(rows[0]["all_day_count"], 1)

        # Union keeps the all-day span from being added on top of the class it covers.
        included = rank.build_rank_rows(members, start, end, include_all_day=True)
        self.assertEqual(included[0]["minutes"], 24 * 60)

    def test_ties_share_a_position(self) -> None:
        members = {
            "1": {
                "name": "小明",
                "events": [_event("数学", "2026-09-01 09:00", "2026-09-01 10:00")],
            },
            "2": {
                "name": "小红",
                "events": [_event("英语", "2026-09-01 09:00", "2026-09-01 10:00")],
            },
            "3": {
                "name": "小刚",
                "events": [_event("物理", "2026-09-01 09:00", "2026-09-01 11:30")],
            },
        }
        rows = rank.build_rank_rows(members, *_bounds("2026-09-01", "2026-09-01"))

        self.assertEqual([row["name"] for row in rows], ["小刚", "小明", "小红"])
        self.assertEqual([row["rank"] for row in rows], [1, 2, 2])

    def test_members_without_courses_are_hidden_unless_requested(self) -> None:
        members = {
            "1": {
                "name": "小明",
                "events": [_event("数学", "2026-09-01 09:00", "2026-09-01 10:00")],
            },
            "2": {"name": "小红", "events": []},
        }
        start, end = _bounds("2026-09-01", "2026-09-01")

        self.assertEqual(
            [row["user_id"] for row in rank.build_rank_rows(members, start, end)], ["1"]
        )

        rows = rank.build_rank_rows(members, start, end, include_empty=True)
        self.assertEqual([row["user_id"] for row in rows], ["1", "2"])
        self.assertEqual(rows[1]["rank"], 0)
        self.assertEqual(rows[1]["minutes"], 0)
        self.assertEqual(rows[1]["hours_text"], "0分钟")

    def test_elapsed_minutes_only_count_finished_courses(self) -> None:
        members = {
            "1": {
                "name": "小明",
                "events": [
                    _event("数学", "2026-09-01 09:00", "2026-09-01 10:00"),
                    _event("英语", "2026-09-03 09:00", "2026-09-03 10:00"),
                ],
            }
        }
        now = datetime(2026, 9, 2, 12, 0, tzinfo=LOCAL_TZ)
        rows = rank.build_rank_rows(members, *_bounds("2026-09-01", "2026-09-07"), now=now)

        self.assertEqual(rows[0]["minutes"], 120)
        self.assertEqual(rows[0]["elapsed_minutes"], 60)
        self.assertEqual(rows[0]["elapsed_text"], "1小时")

    def test_progress_is_relative_to_the_leader(self) -> None:
        members = {
            "1": {
                "name": "小明",
                "events": [_event("数学", "2026-09-01 09:00", "2026-09-01 11:00")],
            },
            "2": {
                "name": "小红",
                "events": [_event("英语", "2026-09-01 09:00", "2026-09-01 10:00")],
            },
        }
        rows = rank.build_rank_rows(members, *_bounds("2026-09-01", "2026-09-01"))
        self.assertEqual([round(row["progress"], 2) for row in rows], [1.0, 0.5])

    def test_excluded_keywords_drop_matching_courses(self) -> None:
        members = {
            "1": {
                "name": "小明",
                "events": [
                    _event("自习", "2026-09-01 09:00", "2026-09-01 11:00"),
                    _event("数学", "2026-09-01 14:00", "2026-09-01 15:00"),
                ],
            }
        }
        rows = rank.build_rank_rows(
            members, *_bounds("2026-09-01", "2026-09-01"), exclude_keywords=["自习"]
        )
        self.assertEqual(rows[0]["minutes"], 60)

    def test_counts_repeated_course_as_one_name(self) -> None:
        members = {
            "1": {
                "name": "小明",
                "events": [
                    _event(
                        "数学",
                        "2026-09-01 09:00",
                        "2026-09-01 10:00",
                        rrule="FREQ=DAILY;COUNT=2",
                    )
                ],
            }
        }
        rows = rank.build_rank_rows(members, *_bounds("2026-09-01", "2026-09-02"))
        self.assertEqual(rows[0]["course_count"], 2)
        self.assertEqual(rows[0]["course_names"], 1)


class RankPeriodTests(unittest.TestCase):
    def _range(self, value: str, today: str = "2026-09-10"):
        return time_range._parse_time_range(value, date.fromisoformat(today))

    def test_relative_periods_resolve_to_full_days(self) -> None:
        cases = {
            "今日": ("2026-09-10", "2026-09-10"),
            "本周": ("2026-09-07", "2026-09-13"),
            "上周": ("2026-08-31", "2026-09-06"),
            "本月": ("2026-09-01", "2026-09-30"),
            "上月": ("2026-08-01", "2026-08-31"),
        }
        for value, (start, end) in cases.items():
            with self.subTest(period=value):
                start_bound, end_bound, label = self._range(value)
                self.assertEqual(start_bound.date(), date.fromisoformat(start))
                self.assertEqual(
                    (end_bound - timedelta(days=1)).date(), date.fromisoformat(end)
                )
                self.assertEqual(label, f"{start}..{end}" if start != end else start)

    def test_explicit_date_range_is_accepted(self) -> None:
        _start, _end, label = self._range("2026-09-01..2026-09-30")
        self.assertEqual(label, "2026-09-01..2026-09-30")

    def test_unknown_period_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self._range("昨天下午")


if __name__ == "__main__":
    unittest.main()
