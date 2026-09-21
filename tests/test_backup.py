"""导入导出：把会话成员打包成 .ics 压缩包或原始备份，再读回来。

Tests cover the archive formats themselves and the service methods the WebUI
calls: what a download contains, what an import writes back, and which files an
import refuses instead of guessing.
"""

from __future__ import annotations

import asyncio
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from _plugin_loader import load_plugin_module

PACKAGE = "course_schedule_backup_test_plugin"
course_schedule = load_plugin_module(PACKAGE, "course_schedule")
sqlite_store = load_plugin_module(PACKAGE, "sqlite_store")
backup = load_plugin_module(PACKAGE, "backup")

SAMPLE_ICS = """BEGIN:VCALENDAR\r
VERSION:2.0\r
X-WR-CALNAME:导入的课表\r
BEGIN:VTIMEZONE\r
TZID:Asia/Shanghai\r
END:VTIMEZONE\r
BEGIN:VEVENT\r
UID:course-1\r
DTSTART;TZID=Asia/Shanghai:20260831T090000\r
DTEND;TZID=Asia/Shanghai:20260831T103000\r
RRULE:FREQ=WEEKLY;BYDAY=MO\r
RDATE;TZID=Asia/Shanghai:20260901T090000\r
SUMMARY:高等数学\r
LOCATION:A101\r
END:VEVENT\r
END:VCALENDAR\r
"""


def _open_zip(content: bytes) -> zipfile.ZipFile:
    return zipfile.ZipFile(io.BytesIO(content))


class ArchiveTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        service = course_schedule.CourseScheduleBase.__new__(
            course_schedule.CourseScheduleBase
        )
        service._schedule_store = sqlite_store.SQLiteScheduleStore(
            db_path=Path(self.temp_dir.name) / "schedule.sqlite3"
        )
        self.service = service

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def run_async(self, coroutine):
        return asyncio.run(coroutine)

    async def add_member(
        self, scope_id: str, user_id: str, name: str, ics: str = SAMPLE_ICS
    ) -> None:
        await self.service._store_member_ics(
            scope_id, user_id, ics, name=name, actor=user_id, uploader_id=user_id
        )

    async def mark(self, scope_id, user_id, day, kind, source_day=""):
        await self.service._schedule_store.set_day_override(
            scope_id,
            user_id,
            day,
            kind,
            source_day=source_day,
            created_by="100",
            created_at="2026-09-21T00:00:00+00:00",
        )


class IcsBundleExportTests(ArchiveTestCase):
    def test_export_member_ics_follows_the_group_file_convention(self) -> None:
        async def scenario() -> None:
            await self.add_member("group:1", "100", "小明")
            export = await self.service._export_member_ics("group:1", "100")
            self.assertEqual(export["filename"], "schedule100.ics")
            self.assertEqual(export["content_type"], "text/calendar; charset=utf-8")
            content = export["content"].decode("utf-8")
            self.assertTrue(content.startswith("BEGIN:VCALENDAR"))
            # The calendar is named after the member, and the recurrence plus
            # the unmodelled RDATE survive the export.
            self.assertIn("X-WR-CALNAME:小明的课表", content)
            self.assertIn("RRULE:FREQ=WEEKLY;BYDAY=MO", content)
            self.assertIn("RDATE;TZID=Asia/Shanghai:20260901T090000", content)

        self.run_async(scenario())

    def test_bundle_holds_one_ics_per_member_plus_a_manifest(self) -> None:
        async def scenario() -> None:
            await self.add_member("group:1", "100", "小明")
            await self.add_member("group:1", "200", "小红")
            export = await self.service._export_scope_archive("group:1", "ics")
            self.assertTrue(export["filename"].startswith("course-schedule-ics-group-1-"))
            self.assertTrue(export["filename"].endswith(".zip"))
            with _open_zip(export["content"]) as archive:
                names = sorted(archive.namelist())
                manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
                self.assertEqual(
                    names,
                    ["README.txt", "manifest.json", "schedule100.ics", "schedule200.ics"],
                )
                self.assertEqual(
                    archive.read("schedule100.ics").decode("utf-8"),
                    (await self.service._export_member_ics("group:1", "100"))["content"].decode("utf-8"),
                )
            self.assertEqual(manifest["format"], backup.ICS_BUNDLE_FORMAT)
            self.assertEqual(manifest["scope_id"], "group:1")
            self.assertEqual(manifest["member_count"], 2)
            self.assertEqual(manifest["event_count"], 2)
            self.assertEqual(
                [(row["user_id"], row["name"]) for row in manifest["members"]],
                [("100", "小明"), ("200", "小红")],
            )

        self.run_async(scenario())

    def test_export_rejects_bad_requests(self) -> None:
        async def scenario() -> None:
            with self.assertRaises(ValueError) as caught:
                await self.service._export_scope_archive("group:1", "ics")
            self.assertIn("还没有成员课表", str(caught.exception))

            await self.add_member("group:1", "100", "小明")
            with self.assertRaises(ValueError) as caught:
                await self.service._export_scope_archive("nonsense", "ics")
            self.assertIn("无效的 scope_id", str(caught.exception))
            with self.assertRaises(ValueError) as caught:
                await self.service._export_scope_archive("group:1", "xml")
            self.assertIn("format", str(caught.exception))
            with self.assertRaises(ValueError) as caught:
                await self.service._export_member_ics("group:1", "999")
            self.assertIn("找不到指定成员", str(caught.exception))

        self.run_async(scenario())


class IcsBundleImportTests(ArchiveTestCase):
    async def _bundle(self) -> bytes:
        await self.add_member("group:1", "100", "小明")
        await self.add_member("group:1", "200", "小红")
        return (await self.service._export_scope_archive("group:1", "ics"))["content"]

    def test_bundle_creates_the_members_it_carries(self) -> None:
        async def scenario() -> None:
            bundle = await self._bundle()
            summary = await self.service._import_scope_archive("group:2", "x.zip", bundle)
            self.assertEqual(summary["format"], "ics")
            self.assertEqual(summary["member_count"], 2)
            self.assertEqual(summary["created_count"], 2)
            self.assertEqual(summary["updated_count"], 0)
            self.assertEqual(summary["event_count"], 2)
            self.assertEqual(summary["skipped"], [])

            members = await self.service._schedule_store.get_scope_members("group:2")
            self.assertEqual(
                {key: value["name"] for key, value in members.items()},
                {"100": "小明", "200": "小红"},
            )
            events = members["100"]["events"]
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["SUMMARY"], "高等数学")
            self.assertIn("RDATE", events[0]["RAW_ICAL"])
            # The .ics bundle carries no day markers; those need the backup.
            self.assertEqual(members["100"]["_day_overrides"], {})

        self.run_async(scenario())

    def test_import_overwrites_events_but_keeps_the_current_name(self) -> None:
        async def scenario() -> None:
            bundle = await self._bundle()
            await self.add_member("group:2", "100", "小明")
            await self.service._edit_schedule_text(
                _FakeEvent("100", group_id="2", admin=True),
                "update",
                person="100",
                member_name="高数课代表",
            )
            summary = await self.service._import_scope_archive("group:2", "x.zip", bundle)
            self.assertEqual(summary["created_count"], 1)
            self.assertEqual(summary["updated_count"], 1)

            members = await self.service._schedule_store.get_scope_members("group:2")
            self.assertEqual(members["100"]["name"], "高数课代表")
            self.assertEqual(len(members["100"]["events"]), 1)

        self.run_async(scenario())

    def test_files_without_a_qq_number_are_reported_not_guessed(self) -> None:
        async def scenario() -> None:
            bundle = await self._bundle()
            with _open_zip(bundle) as archive:
                entries = {name: archive.read(name) for name in archive.namelist()}
            entries["notes.ics"] = entries["schedule200.ics"]
            tampered = backup._zip_bytes(
                list(entries.items()), backup.datetime(2026, 9, 21, 12, 0, 0)
            )

            summary = await self.service._import_scope_archive("group:3", "x.zip", tampered)
            self.assertEqual(summary["member_count"], 2)
            self.assertEqual(len(summary["skipped"]), 1)
            self.assertIn("notes.ics", summary["skipped"][0])
            members = await self.service._schedule_store.get_scope_members("group:3")
            self.assertEqual(set(members), {"100", "200"})

        self.run_async(scenario())

    def test_an_oversized_ics_entry_is_skipped_with_a_reason(self) -> None:
        async def scenario() -> None:
            big = SAMPLE_ICS.replace(
                "END:VEVENT", "DESCRIPTION:" + "x" * (2 * 1024 * 1024) + "\r\nEND:VEVENT"
            )
            bundle = backup._zip_bytes(
                [
                    ("schedule100.ics", SAMPLE_ICS.encode("utf-8")),
                    ("schedule200.ics", big.encode("utf-8")),
                ],
                backup.datetime(2026, 9, 21, 12, 0, 0),
            )
            summary = await self.service._import_scope_archive("group:1", "x.zip", bundle)
            self.assertEqual([item["user_id"] for item in summary["members"]], ["100"])
            self.assertEqual(len(summary["skipped"]), 1)
            self.assertIn("schedule200.ics", summary["skipped"][0])

        self.run_async(scenario())

    def test_a_lone_ics_imports_by_file_name(self) -> None:
        async def scenario() -> None:
            content = (await self.service._store_member_ics(
                "group:1", "100", SAMPLE_ICS, name="小明"
            ))
            self.assertTrue(content["created"])
            summary = await self.service._import_scope_archive(
                "group:2", "schedule100.ics", SAMPLE_ICS.encode("utf-8")
            )
            self.assertEqual(summary["format"], "ics")
            self.assertEqual([item["user_id"] for item in summary["members"]], ["100"])
            members = await self.service._schedule_store.get_scope_members("group:2")
            self.assertEqual(members["100"]["name"], "100")

        self.run_async(scenario())


class SingleIcsImportTests(ArchiveTestCase):
    def test_imports_into_the_selected_member(self) -> None:
        async def scenario() -> None:
            summary = await self.service._import_member_ics("group:1", "200", SAMPLE_ICS)
            self.assertEqual(summary["scope_id"], "group:1")
            self.assertEqual(summary["created_count"], 1)
            self.assertEqual(summary["event_count"], 1)
            info = await self.service._schedule_store.get_member("group:1", "200")
            self.assertEqual(info["name"], "200")
            self.assertEqual(len(info["events"]), 1)

        self.run_async(scenario())

    def test_file_name_outranks_the_selected_member(self) -> None:
        async def scenario() -> None:
            summary = await self.service._import_member_ics(
                "group:1", "200", SAMPLE_ICS, filename="schedule100.ics"
            )
            self.assertEqual([item["user_id"] for item in summary["members"]], ["100"])
            self.assertIsNone(
                await self.service._schedule_store.get_member("group:1", "200")
            )

        self.run_async(scenario())

    def test_needs_a_target_member(self) -> None:
        async def scenario() -> None:
            with self.assertRaises(ValueError) as caught:
                await self.service._import_member_ics("group:1", "", SAMPLE_ICS)
            self.assertIn("schedule<QQ号>.ics", str(caught.exception))
            with self.assertRaises(ValueError) as caught:
                await self.service._import_member_ics("nonsense", "100", SAMPLE_ICS)
            self.assertIn("无效的 scope_id", str(caught.exception))

        self.run_async(scenario())

    def test_rejects_unusable_documents(self) -> None:
        async def scenario() -> None:
            with self.assertRaises(ValueError) as caught:
                await self.service._import_member_ics("group:1", "100", "  ")
            self.assertIn("ICS 内容为空", str(caught.exception))
            with self.assertRaises(ValueError) as caught:
                await self.service._import_member_ics(
                    "group:1", "100", "BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n"
                )
            self.assertIn("没有 VEVENT", str(caught.exception))
            with self.assertRaises(ValueError) as caught:
                await self.service._import_member_ics("group:1", "100", "not a calendar")
            self.assertIn("ICS 解析失败", str(caught.exception))

        self.run_async(scenario())


class BackupArchiveTests(ArchiveTestCase):
    async def _backup(self, scope_id: str = "group:1") -> bytes:
        return (await self.service._export_scope_archive(scope_id, "backup"))["content"]

    async def _source_scope(self) -> None:
        await self.add_member("group:1", "100", "小明")
        await self.add_member("group:1", "200", "小红")
        await self.mark("group:1", "*", "2026-10-01", "holiday")
        await self.mark("group:1", "100", "2026-10-11", "shift", "2026-10-08")

    def test_backup_archive_mirrors_the_storage_layout(self) -> None:
        async def scenario() -> None:
            await self._source_scope()
            content = await self._backup()
            with _open_zip(content) as archive:
                self.assertEqual(
                    sorted(archive.namelist()),
                    [
                        "README.txt",
                        "day_overrides.json",
                        "manifest.json",
                        "members/schedule100.json",
                        "members/schedule200.json",
                    ],
                )
                manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
                member = json.loads(
                    archive.read("members/schedule100.json").decode("utf-8")
                )
                markers = json.loads(archive.read("day_overrides.json").decode("utf-8"))
            self.assertEqual(manifest["format"], backup.BACKUP_FORMAT)
            self.assertEqual(member["user_id"], "100")
            self.assertEqual(member["name"], "小明")
            self.assertEqual(member["info"]["source"], "ics")
            self.assertEqual(len(member["events"]), 1)
            self.assertEqual(member["events"][0]["SUMMARY"], "高等数学")
            self.assertEqual(
                [(row["user_id"], row["day"], row["kind"]) for row in markers],
                [("*", "2026-10-01", "holiday"), ("100", "2026-10-11", "shift")],
            )

        self.run_async(scenario())

    def test_restore_brings_back_events_names_and_markers(self) -> None:
        async def scenario() -> None:
            await self._source_scope()
            content = await self._backup()
            summary = await self.service._import_scope_archive("group:2", "b.zip", content)
            self.assertEqual(summary["format"], "backup")
            self.assertEqual(summary["member_count"], 2)
            self.assertEqual(summary["created_count"], 2)
            self.assertEqual(summary["event_count"], 2)
            self.assertEqual(summary["day_override_count"], 2)

            members = await self.service._schedule_store.get_scope_members("group:2")
            self.assertEqual(
                {key: value["name"] for key, value in members.items()},
                {"100": "小明", "200": "小红"},
            )
            event = members["100"]["events"][0]
            self.assertEqual(event["SUMMARY"], "高等数学")
            self.assertEqual(event["DTSTART"], "20260831T090000")
            self.assertEqual(event["RRULE"], "FREQ=WEEKLY;BYDAY=MO")
            self.assertIn("RDATE", event["RAW_ICAL"])
            self.assertEqual(
                {day: rule["kind"] for day, rule in members["100"]["_day_overrides"].items()},
                {"2026-10-01": "holiday", "2026-10-11": "shift"},
            )
            self.assertEqual(
                members["100"]["_day_overrides"]["2026-10-11"]["source_day"], "2026-10-08"
            )

        self.run_async(scenario())

    def test_restore_matches_the_backup_for_the_members_it_carries(self) -> None:
        async def scenario() -> None:
            await self._source_scope()
            content = await self._backup()

            # The target already has markers the backup does not know about:
            # one for a member the backup carries, one for a member it does not.
            await self.add_member("group:2", "100", "小明")
            await self.mark("group:2", "100", "2026-12-25", "holiday")
            await self.mark("group:2", "999", "2026-12-25", "holiday")

            await self.service._import_scope_archive("group:2", "b.zip", content)
            markers = await self.service._schedule_store.list_day_overrides("group:2")
            self.assertEqual(
                [(row["user_id"], row["day"]) for row in markers],
                [("*", "2026-10-01"), ("100", "2026-10-11"), ("999", "2026-12-25")],
            )

        self.run_async(scenario())

    def test_restore_keeps_the_current_name_and_rebuilds_derived_fields(self) -> None:
        async def scenario() -> None:
            await self._source_scope()
            content = await self._backup()

            await self.add_member("group:2", "100", "小明")
            await self.service._edit_schedule_text(
                _FakeEvent("100", group_id="2", admin=True),
                "update",
                person="100",
                member_name="高数课代表",
            )
            summary = await self.service._import_scope_archive("group:2", "b.zip", content)
            self.assertEqual(summary["updated_count"], 1)

            members = await self.service._schedule_store.get_scope_members("group:2")
            self.assertEqual(members["100"]["name"], "高数课代表")
            self.assertEqual(members["100"]["event_count"], 1)
            self.assertIn("高等数学", members["100"]["schedule"])
            # The rebuilt calendar keeps the base calendar of the original file
            # and still ends with a newline, exactly like a saved schedule.
            self.assertIn("VTIMEZONE", members["100"]["ics"])
            self.assertTrue(members["100"]["ics"].endswith("\r\n"))

        self.run_async(scenario())

    def test_a_member_without_courses_survives_a_backup(self) -> None:
        """An empty schedule is data too: a backup keeps the member row."""

        async def scenario() -> None:
            await self.add_member("group:1", "100", "小明")
            await self.service._create_member_schedules(
                "group:1", [{"user_id": "200", "name": "小红"}]
            )
            content = await self._backup()
            summary = await self.service._import_scope_archive("group:2", "b.zip", content)
            self.assertEqual(summary["member_count"], 2)
            self.assertEqual(summary["event_count"], 1)

            members = await self.service._schedule_store.get_scope_members("group:2")
            self.assertEqual(members["200"]["name"], "小红")
            self.assertEqual(members["200"]["events"], [])
            self.assertTrue(members["200"]["ics"].startswith("BEGIN:VCALENDAR"))

            # The .ics bundle cannot carry a course-free member, so it reports
            # that file instead of creating a member it has nothing for.
            bundle = (await self.service._export_scope_archive("group:1", "ics"))["content"]
            summary = await self.service._import_scope_archive("group:3", "a.zip", bundle)
            self.assertEqual([item["user_id"] for item in summary["members"]], ["100"])
            self.assertEqual(len(summary["skipped"]), 1)
            self.assertIn("schedule200.ics", summary["skipped"][0])

        self.run_async(scenario())

    def test_restore_ignores_markers_of_unknown_kind(self) -> None:
        async def scenario() -> None:
            await self._source_scope()
            content = await self._backup()
            with _open_zip(content) as archive:
                entries = {name: archive.read(name) for name in archive.namelist()}
            entries["day_overrides.json"] = json.dumps(
                [
                    {"user_id": "100", "day": "2026-10-12", "kind": "junk"},
                    {"user_id": "100", "day": "2026-10-13", "kind": "holiday"},
                ]
            ).encode("utf-8")
            tampered = backup._zip_bytes(
                list(entries.items()), backup.datetime(2026, 9, 21, 12, 0, 0)
            )

            summary = await self.service._import_scope_archive("group:2", "b.zip", tampered)
            self.assertEqual(summary["day_override_count"], 1)
            markers = await self.service._schedule_store.list_day_overrides("group:2")
            self.assertEqual(
                [(row["user_id"], row["day"], row["kind"]) for row in markers],
                [("100", "2026-10-13", "holiday")],
            )

        self.run_async(scenario())


class ArchiveRejectionTests(ArchiveTestCase):
    def test_a_document_that_is_not_a_zip_is_rejected(self) -> None:
        async def scenario() -> None:
            with self.assertRaises(ValueError) as caught:
                await self.service._import_scope_archive(
                    "group:1", "notes.txt", b"hello"
                )
            self.assertIn("不是有效的 .zip", str(caught.exception))
            with self.assertRaises(ValueError) as caught:
                await self.service._import_scope_archive("group:1", "empty.zip", b"")
            self.assertIn("是空的", str(caught.exception))

        self.run_async(scenario())

    def test_a_zip_without_a_manifest_or_ics_is_rejected(self) -> None:
        async def scenario() -> None:
            content = backup._zip_bytes(
                [("notes.txt", b"hello")], backup.datetime(2026, 9, 21, 12, 0, 0)
            )
            with self.assertRaises(ValueError) as caught:
                await self.service._import_scope_archive("group:1", "x.zip", content)
            self.assertIn("没有 manifest.json", str(caught.exception))

        self.run_async(scenario())

    def test_a_damaged_manifest_falls_back_to_the_file_names(self) -> None:
        async def scenario() -> None:
            content = backup._zip_bytes(
                [
                    ("manifest.json", b"{not json"),
                    ("schedule100.ics", SAMPLE_ICS.encode("utf-8")),
                ],
                backup.datetime(2026, 9, 21, 12, 0, 0),
            )
            summary = await self.service._import_scope_archive("group:1", "x.zip", content)
            self.assertEqual([item["user_id"] for item in summary["members"]], ["100"])
            members = await self.service._schedule_store.get_scope_members("group:1")
            # Without a manifest there is no name for the member.
            self.assertEqual(members["100"]["name"], "100")

        self.run_async(scenario())

    def test_too_many_entries_are_rejected(self) -> None:
        async def scenario() -> None:
            content = backup._zip_bytes(
                [(f"f{index}.txt", b"x") for index in range(4)],
                backup.datetime(2026, 9, 21, 12, 0, 0),
            )
            with (
                mock.patch.object(backup, "MAX_ARCHIVE_ENTRIES", 2),
                self.assertRaises(ValueError) as caught,
            ):
                await self.service._import_scope_archive("group:1", "x.zip", content)
            self.assertIn("文件过多", str(caught.exception))
        self.run_async(scenario())

    def test_an_archive_that_unpacks_too_large_is_rejected(self) -> None:
        async def scenario() -> None:
            content = backup._zip_bytes(
                [("schedule100.ics", b"x" * 4096)],
                backup.datetime(2026, 9, 21, 12, 0, 0),
            )
            with (
                mock.patch.object(backup, "MAX_ARCHIVE_UNPACKED_BYTES", 1024),
                self.assertRaises(ValueError) as caught,
            ):
                await self.service._import_scope_archive("group:1", "x.zip", content)
            self.assertIn("解压后", str(caught.exception))
            with (
                mock.patch.object(backup, "MAX_ARCHIVE_BYTES", 16),
                self.assertRaises(ValueError) as caught,
            ):
                await self.service._import_scope_archive("group:1", "x.zip", content)
            self.assertIn("压缩包超过", str(caught.exception))

        self.run_async(scenario())

    def test_an_archive_whose_members_all_fail_reports_why(self) -> None:
        async def scenario() -> None:
            content = backup._zip_bytes(
                [
                    ("manifest.json", json.dumps({"format": "course-schedule-backup"}).encode()),
                    ("members/schedule100.json", b"{not json"),
                ],
                backup.datetime(2026, 9, 21, 12, 0, 0),
            )
            with self.assertRaises(ValueError) as caught:
                await self.service._import_scope_archive("group:1", "x.zip", content)
            self.assertIn("没有可导入的成员记录", str(caught.exception))

        self.run_async(scenario())


class _FakeEvent:
    """The minimum of an AstrMessageEvent the edit tool touches."""

    def __init__(self, sender_id: str, *, group_id: str = "1", admin: bool = False):
        self.sender_id = sender_id
        self.group_id = group_id
        self.admin = admin

    def get_sender_id(self) -> str:
        return self.sender_id

    def get_sender_name(self) -> str:
        return "小明" if self.sender_id == "100" else self.sender_id

    def get_group_id(self) -> str:
        return self.group_id

    def get_messages(self) -> list[object]:
        return []

    def is_admin(self) -> bool:
        return self.admin


if __name__ == "__main__":
    unittest.main()
