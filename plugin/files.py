from __future__ import annotations

from pathlib import Path

from .constants import PLUGIN_ID


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
