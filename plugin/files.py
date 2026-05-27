from __future__ import annotations

import base64
import tempfile
from pathlib import Path
from typing import Any
from urllib.request import urlopen

from .constants import (
    MAX_ICS_BYTES,
    PLUGIN_ID,
    ROOT_SCHEDULE_FILE_RE,
    SCHEDULE_FOLDER_FILE_RE,
    SCHEDULE_FOLDER_NAME,
)


def _extract_schedule_user_id(filename: str, folder_path: str = "") -> str | None:
    name = filename.strip()
    normalized_folder_path = folder_path.strip("/").lower()

    if normalized_folder_path.split("/")[-1:] == [SCHEDULE_FOLDER_NAME]:
        match = SCHEDULE_FOLDER_FILE_RE.match(name)
        if match:
            return match.group(1)

    match = ROOT_SCHEDULE_FILE_RE.match(name)
    if match:
        return match.group(1)

    return None


def _file_name(file_info: dict[str, Any]) -> str:
    return str(file_info.get("file_name") or file_info.get("name") or "")


def _folder_name(folder_info: dict[str, Any]) -> str:
    return str(folder_info.get("folder_name") or folder_info.get("name") or "")


def _join_folder_path(parent: str, name: str) -> str:
    if not parent:
        return name

    return f"{parent.rstrip('/')}/{name}"


def _display_group_file_path(file_info: dict[str, Any]) -> str:
    folder_path = str(file_info.get("_folder_path") or "").strip("/")
    filename = _file_name(file_info)
    if not folder_path:
        return filename

    return f"{folder_path}/{filename}"


def _normalized_schedule_filename(user_id: str) -> str:
    return f"{user_id}.ics"


def _normalized_schedule_path(user_id: str) -> str:
    return f"{SCHEDULE_FOLDER_NAME}/{_normalized_schedule_filename(user_id)}"


def _is_normalized_schedule_file(file_info: dict[str, Any], user_id: str) -> bool:
    folder_path = str(file_info.get("_folder_path") or "").strip("/").lower()
    return (
        folder_path == SCHEDULE_FOLDER_NAME
        and _file_name(file_info).lower() == _normalized_schedule_filename(user_id).lower()
    )


def _download_text(url: str, max_bytes: int = MAX_ICS_BYTES) -> str:
    with urlopen(url, timeout=20) as response:
        data = response.read(max_bytes + 1)

    if len(data) > max_bytes:
        raise ValueError("文件过大")

    return data.decode("utf-8-sig")


def _write_temp_ics(filename: str, content: str) -> str:
    temp_dir = Path(tempfile.gettempdir()) / PLUGIN_ID
    temp_dir.mkdir(parents=True, exist_ok=True)
    target = temp_dir / filename
    target.write_text(content, encoding="utf-8")
    return str(target)


def _write_temp_schedule_ics(user_id: str, content: str) -> tuple[str, str]:
    filename = _normalized_schedule_filename(user_id)
    return filename, _write_temp_ics(filename, content)


def _local_file_uri(path: str) -> str:
    return Path(path).resolve().as_uri()


def _inline_file_uris(content: str) -> list[str]:
    encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
    return [
        f"base64://{encoded}",
        f"data:text/calendar;charset=utf-8;base64,{encoded}",
    ]


def _asset_temp_path(filename: str) -> str:
    temp_dir = Path(tempfile.gettempdir()) / PLUGIN_ID
    temp_dir.mkdir(parents=True, exist_ok=True)
    return str(temp_dir / filename)
