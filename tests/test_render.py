"""Regression tests for the card renderer.

Rendering is the plugin's main output, so these tests pin the things that
silently break it: the card geometry, the Emoji fallback for nicknames the CJK
font cannot draw, and the two caches (avatars, output images) that must stay
bounded.  They also time a render loosely enough to catch an accidental
per-glyph drawing loop, which is what made the card slow before.
"""

from __future__ import annotations

import time
import unittest
from datetime import date
from pathlib import Path

from _plugin_loader import load_plugin_module

PACKAGE = "course_schedule_render_test_plugin"
render = load_plugin_module(PACKAGE, "render")


def _row(index: int, name: str = "", status_key: str = "active") -> dict[str, object]:
    return {
        "user_id": str(100000 + index),
        "name": name or f"同学{index}",
        "status": "正在上课",
        "status_key": status_key,
        "course": "高等数学",
        "location": "教学楼A101",
        "time": "09:00 - 10:30",
        "duration": "1小时30分钟",
        "countdown_label": "距下课",
        "countdown": "1小时",
        "progress": 0.5,
    }


def _rows(count: int = 6) -> list[dict[str, object]]:
    statuses = ["active", "upcoming", "finished", "none", "holiday", "scheduled"]
    return [_row(index, status_key=statuses[index % len(statuses)]) for index in range(count)]


class RenderCardTests(unittest.TestCase):
    def setUp(self) -> None:
        # No network in tests: every avatar falls back to the initials circle.
        self._real_download = render._download_avatar
        render._download_avatar = lambda user_id, size: None
        render._AVATAR_MEMORY_CACHE.clear()

    def tearDown(self) -> None:
        render._download_avatar = self._real_download
        render._AVATAR_MEMORY_CACHE.clear()

    def test_renders_one_card_per_member(self) -> None:
        path = render._draw_rows_image(
            "课程表 · 2026-09-19 周六", _rows(6), "render_test.png", footer="实时状态"
        )
        saved = Path(path)
        self.assertTrue(saved.is_file())
        self.assertEqual(saved.suffix, render.IMAGE_EXTENSION)

        from PIL import Image

        with Image.open(saved) as image:
            self.assertEqual(image.mode, "RGB")
            # Cards grow downwards, never sideways.
            self.assertEqual(image.width, 1240)
            self.assertGreater(image.height, 500)

    def test_empty_member_list_still_renders_a_placeholder(self) -> None:
        path = render._draw_rows_image(
            "课程表 · 2026-09-19 周六", [], "render_empty.png", footer="实时状态"
        )
        self.assertTrue(Path(path).is_file())

    def test_emoji_nicknames_are_drawn_with_a_fallback_font(self) -> None:
        """A ZWJ sequence and a flag must not become tofu boxes."""
        primary = render._load_font(27, bold=True)
        for cluster in ("👩🏽‍💻", "🇨🇳", "😀"):
            font, is_emoji, scale = render._font_for_cluster(cluster, 27, True)
            self.assertTrue(is_emoji, f"{cluster} should use the Emoji fallback")
            self.assertIsNot(font, primary)
            self.assertGreater(scale, 0.0)

    def test_wrapped_name_measurement_matches_what_is_drawn(self) -> None:
        long_name = "名字特别特别特别特别长的同学" * 2
        lines = render._wrap_rich_text(long_name, 27, 235, True)
        self.assertGreater(len(lines), 1)
        for line in lines:
            self.assertLessEqual(render._rich_width(line, 27, True), 235)

    def test_card_rendering_stays_fast_enough(self) -> None:
        """Guards against regressing to one FreeType call per glyph.

        Drawing a 20-member board character by character measured ~135 ms on
        the development machine; drawing each same-font run in one call is
        ~65 ms.  The bound is deliberately loose so a slower CI machine or a
        cold font cache does not fail the suite.
        """
        render._draw_rows_image("课程表", _rows(20), "render_speed.png", footer="实时状态")
        started = time.perf_counter()
        render._draw_rows_image("课程表", _rows(20), "render_speed.png", footer="实时状态")
        elapsed = time.perf_counter() - started
        self.assertLess(elapsed, 1.0, f"20-member card took {elapsed * 1000:.0f} ms")


class AvatarCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self._real_download = render._download_avatar
        render._AVATAR_MEMORY_CACHE.clear()

    def tearDown(self) -> None:
        render._download_avatar = self._real_download
        render._AVATAR_MEMORY_CACHE.clear()

    def test_downloads_once_and_serves_the_memory_hit_afterwards(self) -> None:
        calls: list[str] = []

        def counting_download(user_id: str, size: int):
            """Stand in for a download that fails, so the fallback is used."""
            calls.append(user_id)

        render._download_avatar = counting_download
        first = render._fetch_avatar("10001", render.AVATAR_SIZE)
        second = render._fetch_avatar("10001", render.AVATAR_SIZE)
        self.assertEqual(calls, ["10001"])
        self.assertIs(first, second)

    def test_failed_download_is_only_cached_briefly(self) -> None:
        """A dead avatar host must not cost a timeout on every render."""
        calls: list[str] = []
        render._download_avatar = lambda user_id, size: (calls.append(user_id), None)[1]

        render._fetch_avatar("10002", render.AVATAR_SIZE)
        render._fetch_avatar("10002", render.AVATAR_SIZE)
        self.assertEqual(calls, ["10002"])

        # Once the short failure TTL has passed the avatar is retried.
        key = ("10002", render.AVATAR_SIZE)
        expiry, avatar = render._AVATAR_MEMORY_CACHE[key]
        render._AVATAR_MEMORY_CACHE[key] = (expiry - render.AVATAR_FAILURE_TTL_SECONDS - 1, avatar)
        render._fetch_avatar("10002", render.AVATAR_SIZE)
        self.assertEqual(calls, ["10002", "10002"])

    def test_disk_cache_expires_with_the_ttl(self) -> None:
        avatar = render._fallback_avatar("10003", render.AVATAR_SIZE)
        path = render._avatar_cache_path("10003", render.AVATAR_SIZE)
        render._write_avatar_cache(path, avatar)
        self.assertIsNotNone(render._read_avatar_cache(path))

        import os

        stale = time.time() - render.AVATAR_CACHE_TTL_SECONDS - 60
        os.utime(path, (stale, stale))
        self.assertIsNone(render._read_avatar_cache(path))
        path.unlink(missing_ok=True)

    def test_memory_cache_is_bounded(self) -> None:
        render._download_avatar = lambda user_id, size: None
        for index in range(render._AVATAR_MEMORY_CACHE_MAX + 10):
            render._fetch_avatar(str(20000 + index), render.AVATAR_SIZE)
        self.assertLessEqual(
            len(render._AVATAR_MEMORY_CACHE), render._AVATAR_MEMORY_CACHE_MAX
        )

    def test_prune_removes_expired_avatars_and_orphan_temp_files(self) -> None:
        import os
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fresh = root / "fresh_76.png"
            stale = root / "stale_76.png"
            orphan = root / "leftover.png.123.456.tmp"
            for path in (fresh, stale, orphan):
                path.write_bytes(b"x")
            old = time.time() - render.AVATAR_CACHE_TTL_SECONDS - 60
            os.utime(stale, (old, old))
            os.utime(orphan, (old, old))

            render._prune_avatar_cache(root)

            self.assertTrue(fresh.is_file())
            self.assertFalse(stale.exists())
            self.assertFalse(orphan.exists())


class FooterTests(unittest.TestCase):
    def test_footer_names_the_day_being_shown(self) -> None:
        today = date(2026, 9, 19)
        self.assertEqual(render.schedule_footer(today, today), render.FOOTER_LIVE)
        self.assertEqual(
            render.schedule_footer(date(2026, 9, 18), today), render.FOOTER_ARCHIVE
        )
        self.assertEqual(
            render.schedule_footer(date(2026, 9, 20), today), render.FOOTER_PLANNED
        )


if __name__ == "__main__":
    unittest.main()
