"""Load the plugin's modules without an AstrBot installation.

The modules in ``plugin/`` import only each other, so a test can expose that
directory as a package and import the one module it needs: every dependency
comes along through the normal relative imports.  AstrBot itself is not
installed when the tests run, so a minimal stub of the modules the plugin
imports at module scope is registered first, and the plugin data directory is
redirected to a throwaway temporary directory.

Every test file passes its own ``package_name`` so the modules it loads live in
their own namespace in ``sys.modules`` instead of colliding with another file's.
"""

from __future__ import annotations

import importlib
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "plugin"

# One throwaway data directory per test session; the plugin writes its SQLite
# store, generated images and avatar cache underneath it.
_DATA_DIR = tempfile.mkdtemp(prefix="course_schedule_test_")


def install_astrbot_stubs() -> None:
    """Register the minimal ``astrbot`` modules the plugin imports."""
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    event = types.ModuleType("astrbot.api.event")
    event.AstrMessageEvent = type("AstrMessageEvent", (), {})
    api.event = event
    core = types.ModuleType("astrbot.core")
    utils = types.ModuleType("astrbot.core.utils")
    path_module = types.ModuleType("astrbot.core.utils.astrbot_path")
    path_module.get_astrbot_data_path = lambda: _DATA_DIR
    # setdefault so a test that installs a richer stub (message components, for
    # example) keeps it while the pieces it does not define still appear.
    for name, module in (
        ("astrbot", astrbot),
        ("astrbot.api", api),
        ("astrbot.api.event", event),
        ("astrbot.core", core),
        ("astrbot.core.utils", utils),
        ("astrbot.core.utils.astrbot_path", path_module),
    ):
        sys.modules.setdefault(name, module)


def load_plugin_module(package_name: str, module_name: str) -> types.ModuleType:
    """Import ``plugin/<module_name>.py`` inside a fake package.

    Returns the loaded module; any module it imports relatively is loaded the
    same way and can afterwards be fetched with ``importlib.import_module`` or
    by asking for it again here.
    """
    install_astrbot_stubs()
    if package_name not in sys.modules:
        package = types.ModuleType(package_name)
        package.__path__ = [str(PLUGIN_DIR)]
        sys.modules[package_name] = package
    return importlib.import_module(f"{package_name}.{module_name}")
