from __future__ import annotations

import re
from pathlib import Path
from zoneinfo import ZoneInfo

PLUGIN_ID = "astrbot_plugin_course_schedule"
STORE_KEY = "course_schedule:v1"
PLUGIN_DIR = Path(__file__).resolve().parents[1]
FONT_DIR = PLUGIN_DIR / "assets" / "fonts"
ROOT_SCHEDULE_FILE_RE = re.compile(r"^schedule(\d+)\.ics$", re.IGNORECASE)
SCHEDULE_FOLDER_FILE_RE = re.compile(r"^(\d+)\.ics$", re.IGNORECASE)
SCHEDULE_FOLDER_NAME = "schedule"
MAX_ICS_BYTES = 2 * 1024 * 1024
MAX_EVENTS_PER_FILE = 120
LOCAL_TZ = ZoneInfo("Asia/Shanghai")
WEEKDAY_CODES = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]
