"""Members whose day is over are folded into a compact strip below the cards."""

from __future__ import annotations

import unittest
from datetime import date, datetime
from pathlib import Path

from _plugin_loader import load_plugin_module

PACKAGE = "course_schedule_folded_test_plugin"
domain = load_plugin_module(PACKAGE, "domain")
render = load_plugin_module(PACKAGE, "render")
LOCAL_TZ = load_plugin_module(PACKAGE, "constants").LOCAL_TZ

DAY = date(2026, 9, 19)
# 14:30: the morning class is over, the evening one has not started.
AFTERNOON = datetime(2026, 9, 19, 14, 30, tzinfo=LOCAL_TZ)
LATE = datetime(2026, 9, 19, 22, 0, tzinfo=LOCAL_TZ)


def _member(name: str, *courses: tuple[int, int, str]) -> dict[str, object]:
    return {
        "name": name,
        "events": [
            domain.make_event(course, f"2026-09-19 {start:02d}:00", f"2026-09-19 {end:02d}:00")
            for start, end, course in courses
        ],
    }


def _split(members: dict[str, object], when: datetime, day: date = DAY):
    rows = domain.daily_member_rows(members, day, now=when)
    return domain.split_folded_rows(rows)


class SplitFoldedRowsTests(unittest.TestCase):
    def test_finished_and_idle_members_fold_while_active_ones_keep_cards(self) -> None:
        members = {
            "1": _member("正在上课", (13, 16, "数学")),
            "2": _member("下一节", (16, 17, "英语")),
            "3": _member("已上完", (8, 9, "体育")),
            "4": _member("没课"),
        }
        shown, folded = _split(members, AFTERNOON)

        self.assertEqual([row["status_key"] for row in shown], ["active", "upcoming"])
        self.assertEqual([row["status_key"] for row in folded], ["finished", "none"])
        self.assertEqual([row["name"] for row in folded], ["已上完", "没课"])

    def test_holiday_members_fold_too(self) -> None:
        members = {
            "1": _member("上课", (13, 16, "数学")),
            "2": {**_member("休假", (13, 16, "英语")),
                  "_day_overrides": {"2026-09-19": {"kind": "holiday", "source_day": ""}}},
        }
        shown, folded = _split(members, AFTERNOON)
        self.assertEqual([row["name"] for row in shown], ["上课"])
        self.assertEqual([row["name"] for row in folded], ["休假"])

    def test_everyone_folds_once_the_day_is_over(self) -> None:
        members = {"1": _member("甲", (8, 9, "数学")), "2": _member("乙")}
        shown, folded = _split(members, LATE)
        self.assertEqual(shown, [])
        self.assertEqual(len(folded), 2)

    def test_nothing_folds_when_nobody_is_idle(self) -> None:
        members = {"1": _member("甲", (13, 16, "数学")), "2": _member("乙", (16, 17, "英语"))}
        shown, folded = _split(members, AFTERNOON)
        self.assertEqual(len(shown), 2)
        self.assertEqual(folded, [])

    def test_a_future_day_keeps_a_full_card_per_member(self) -> None:
        """A plan is read for its countdown, so it must not be folded away."""
        members = {"1": _member("甲"), "2": _member("乙", (9, 10, "数学"))}
        for day in (date(2026, 9, 20), date(2026, 9, 18)):
            shown, folded = _split(members, AFTERNOON, day)
            self.assertEqual(len(shown), 2, f"{day} should not fold")
            self.assertEqual(folded, [], f"{day} should not fold")

    def test_empty_input_is_handled(self) -> None:
        self.assertEqual(domain.split_folded_rows([]), ([], []))


class FoldedStripRenderTests(unittest.TestCase):
    def setUp(self) -> None:
        self._real_download = render._download_avatar
        render._download_avatar = lambda user_id, size: None
        render._AVATAR_MEMORY_CACHE.clear()

    def tearDown(self) -> None:
        render._download_avatar = self._real_download
        render._AVATAR_MEMORY_CACHE.clear()

    def _card_row(self, index: int) -> dict[str, object]:
        return {
            "user_id": str(900000 + index),
            "name": f"上课{index}",
            "status": "下一节即将上课",
            "status_key": "upcoming",
            "course": "数学",
            "location": "",
            "time": "16:00 - 17:00",
            "duration": "1小时",
            "countdown_label": "距上课",
            "countdown": "1小时",
            "progress": 0.0,
        }

    def _folded_row(self, index: int) -> dict[str, object]:
        return {
            "user_id": str(800000 + index),
            "name": f"没课{index}",
            "status_key": "none",
        }

    def test_folded_strip_is_much_shorter_than_cards(self) -> None:
        folded = [self._folded_row(i) for i in range(20)]
        with_strip = Path(
            render._draw_rows_image(
                "课程表", [self._card_row(0)], "folded_strip.png", folded=folded
            )
        )
        without = Path(
            render._draw_rows_image("课程表", [self._card_row(0)], "no_strip.png")
        )
        from PIL import Image

        with Image.open(with_strip) as a, Image.open(without) as b:
            self.assertEqual(a.width, b.width)
            # 20 folded members at 5 per row cost 4 rows instead of 20 cards.
            strip = render._folded_height(20, 1240 - 36 * 2)
            self.assertEqual(strip, 332)
            # The gap between cards and the strip is part of the extra height.
            self.assertEqual(a.height, b.height + strip + 16)

    def test_an_image_with_only_folded_members_still_renders(self) -> None:
        path = Path(
            render._draw_rows_image(
                "课程表", [], "only_folded.png", folded=[self._folded_row(i) for i in range(3)]
            )
        )
        self.assertTrue(path.is_file())

    def test_grid_grows_a_row_at_a_time(self) -> None:
        inner = 1240 - 36 * 2
        columns = render._folded_columns(inner)
        self.assertGreater(columns, 1)
        one_row = render._folded_height(columns, inner)
        two_rows = render._folded_height(columns + 1, inner)
        self.assertGreater(two_rows, one_row)
        self.assertEqual(render._folded_height(0, inner), 0)


if __name__ == "__main__":
    unittest.main()
