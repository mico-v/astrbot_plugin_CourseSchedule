from __future__ import annotations

import sys
import tempfile
import types
import unittest
from pathlib import Path

from _plugin_loader import install_astrbot_stubs, load_plugin_module


def _install_message_component_stub() -> None:
    """AstrBot's File component is what the extractor walks, so stub it first."""
    components = types.ModuleType("astrbot.api.message_components")

    class FakeFile:
        def __init__(self, name: str, file: str = ""):
            self.name = name
            self.file = file

        async def get_file(self):
            return self.file

    components.File = FakeFile
    components.Plain = type("Plain", (), {})
    sys.modules["astrbot.api.message_components"] = components


_install_message_component_stub()
install_astrbot_stubs()
message_files = load_plugin_module("course_schedule_message_test_plugin", "message_files")


class MessageFileExtractionTests(unittest.TestCase):
    class Event:
        def __init__(self, *messages):
            self.messages = messages

        def get_messages(self):
            return list(self.messages)

    def test_extracts_inline_attachment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fall.ics"
            path.write_bytes(b"BEGIN:VCALENDAR\nEND:VCALENDAR")
            event = self.Event(message_files.File(name="fall.ics", file=str(path)))
            self.assertEqual(
                __import__("asyncio").run(message_files.extract_ics_from_event(event)),
                ("BEGIN:VCALENDAR\nEND:VCALENDAR", "fall.ics"),
            )

    def test_extracts_local_ics_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "schedule.ics"
            path.write_text("BEGIN:VCALENDAR\nEND:VCALENDAR", encoding="utf-8")
            event = self.Event(message_files.File(name="schedule.ics", file=str(path)))
            content, filename = __import__("asyncio").run(message_files.extract_ics_from_event(event))
            self.assertIn("BEGIN:VCALENDAR", content)
            self.assertEqual(filename, "schedule.ics")


if __name__ == "__main__":
    unittest.main()
