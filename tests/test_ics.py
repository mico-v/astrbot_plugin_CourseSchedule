from __future__ import annotations

import unittest
from datetime import datetime

from _plugin_loader import load_plugin_module

PACKAGE = "course_schedule_ics_test_plugin"
ics = load_plugin_module(PACKAGE, "ics")
occurrences = load_plugin_module(PACKAGE, "occurrences")
LOCAL_TZ = load_plugin_module(PACKAGE, "constants").LOCAL_TZ


SAMPLE_ICS = """BEGIN:VCALENDAR\r
VERSION:2.0\r
X-WR-CALNAME:2026 秋季课表\r
BEGIN:VEVENT\r
UID:course-1\r
DTSTART;TZID=Asia/Shanghai:20260831T090000\r
DTEND;TZID=Asia/Shanghai:20260831T103000\r
RRULE:FREQ=WEEKLY;BYDAY=MO,WE;COUNT=4\r
EXDATE;TZID=Asia/Shanghai:20260902T090000\r
SUMMARY:高等数学\r
LOCATION:A101\r
X-CUSTOM-PROPERTY:keep-me\r
END:VEVENT\r
END:VCALENDAR\r
"""


class StandardICSTests(unittest.TestCase):
    def test_parse_and_expand_rrule_with_exdate(self) -> None:
        events, _text = ics._parse_schedule_ics(SAMPLE_ICS)
        expanded = occurrences._expand_member_occurrences(
            {"events": events},
            datetime(2026, 8, 30, tzinfo=LOCAL_TZ),
            datetime(2026, 9, 15, tzinfo=LOCAL_TZ),
        )
        starts = [item["_start"].strftime("%Y-%m-%d %H:%M") for item in expanded]
        self.assertEqual(
            starts,
            ["2026-08-31 09:00", "2026-09-07 09:00", "2026-09-09 09:00"],
        )

    def test_serialize_preserves_unknown_and_exception_properties(self) -> None:
        events, _text = ics._parse_schedule_ics(SAMPLE_ICS)
        serialized = ics._serialize_schedule_ics(events)
        self.assertIn("X-CUSTOM-PROPERTY:keep-me", serialized)
        self.assertIn("EXDATE;TZID=Asia/Shanghai:20260902T090000", serialized)

    def test_rewriting_an_edited_event_keeps_extensions_and_calendar_properties(self) -> None:
        """Every edit path reserializes the event list through this function."""
        events, _text = ics._parse_schedule_ics(SAMPLE_ICS)
        events[0]["SUMMARY"] = "线性代数"
        content = ics._serialize_schedule_ics(events, SAMPLE_ICS)
        reparsed, _text = ics._parse_schedule_ics(content)
        self.assertEqual(reparsed[0]["SUMMARY"], "线性代数")
        self.assertIn("X-CUSTOM-PROPERTY:keep-me", content)
        self.assertIn("EXDATE;TZID=Asia/Shanghai:20260902T090000", content)
        self.assertIn("X-WR-CALNAME:2026 秋季课表", content)


if __name__ == "__main__":
    unittest.main()
