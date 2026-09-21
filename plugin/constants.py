from __future__ import annotations

from pathlib import Path
from zoneinfo import ZoneInfo

PLUGIN_ID = "astrbot_plugin_course_schedule"
PLUGIN_DIR = Path(__file__).resolve().parents[1]
FONT_DIR = PLUGIN_DIR / "assets" / "fonts"
MAX_ICS_BYTES = 2 * 1024 * 1024
MAX_EVENTS_PER_FILE = 120
MAX_MEMBERS_PER_CREATE = 200
LOCAL_TZ = ZoneInfo("Asia/Shanghai")

# Limits for the WebUI import/export archives: the uploaded zip itself, the
# bytes it may unpack to (a small zip can expand a lot) and how many files it
# may contain.
MAX_ARCHIVE_BYTES = 16 * 1024 * 1024
MAX_ARCHIVE_UNPACKED_BYTES = 32 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 500

# Day-off (休假) and make-up (调休) markers.
DAY_OVERRIDE_HOLIDAY = "holiday"
DAY_OVERRIDE_SHIFT = "shift"
# Stored in the marker's user_id column to mean "every member of the scope",
# so members that register later inherit the marker too.
DAY_OVERRIDE_ALL = "*"
MAX_DAY_OVERRIDES_PER_SCOPE = 1000
MAX_DAY_OVERRIDE_RANGE_DAYS = 180
MAX_DAY_OVERRIDE_SPAN_DAYS = 366
