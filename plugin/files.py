from __future__ import annotations

import time
from pathlib import Path

from .constants import PLUGIN_ID


# A generated export is only needed until the browser has fetched it; older
# files in the directory are leftovers from a cancelled download.
EXPORT_MAX_AGE_SECONDS = 60 * 60


def _plugin_data_dir() -> Path:
    """Return AstrBot's persistent plugin-data directory.

    Large plugin files belong under ``data/plugin_data/<plugin>/`` as required
    by AstrBot: https://docs.astrbot.app/dev/star/guides/storage.html
    """
    try:
        from astrbot.api.star import StarTools

        return Path(StarTools.get_data_dir(PLUGIN_ID))
    except Exception:
        # Fallback keeps development/test environments usable and follows the
        # documented AstrBot layout.
        from astrbot.core.utils.astrbot_path import get_astrbot_data_path

        path = Path(get_astrbot_data_path()) / "plugin_data" / PLUGIN_ID
        path.mkdir(parents=True, exist_ok=True)
        return path


def _plugin_data_dir_path(*parts: str) -> Path:
    """Return a directory under the plugin data directory, creating it."""
    path = _plugin_data_dir().joinpath(*parts)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _write_export_file(filename: str, content: bytes) -> Path:
    """Write one download to the export directory and return its path.

    The name is reduced to its final component so a caller-supplied name can
    never escape the export directory.
    """
    directory = _plugin_data_dir_path("exports")
    _prune_exports(directory)
    path = directory / Path(str(filename or "export.bin")).name
    path.write_bytes(content)
    return path


def _prune_exports(directory: Path) -> None:
    now = time.time()
    try:
        for item in directory.iterdir():
            if item.is_file() and now - item.stat().st_mtime > EXPORT_MAX_AGE_SECONDS:
                item.unlink(missing_ok=True)
    except OSError:
        pass
