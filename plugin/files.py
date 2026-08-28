from __future__ import annotations

import tempfile
from pathlib import Path

from .constants import PLUGIN_ID


def _asset_temp_path(filename: str) -> str:
    temp_dir = Path(tempfile.gettempdir()) / PLUGIN_ID
    temp_dir.mkdir(parents=True, exist_ok=True)
    return str(temp_dir / filename)
