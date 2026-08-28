from __future__ import annotations

from pathlib import Path
from zoneinfo import ZoneInfo

PLUGIN_ID = "astrbot_plugin_course_schedule"
PLUGIN_DIR = Path(__file__).resolve().parents[1]
FONT_DIR = PLUGIN_DIR / "assets" / "fonts"
MAX_ICS_BYTES = 2 * 1024 * 1024
MAX_EVENTS_PER_FILE = 120
LOCAL_TZ = ZoneInfo("Asia/Shanghai")
