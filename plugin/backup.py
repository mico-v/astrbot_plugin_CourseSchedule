"""导入导出：把一个会话的成员打包成压缩包，以及把压缩包读回来。

WebUI 提供两种导出格式，两个压缩包里都会有一份 ``manifest.json``，记录格式、
版本、导出时间和成员名单：

* ``ics``    —— 每位成员一个 ``schedule<QQ号>.ics`` 文件。文件名沿用群文件约定，
                 所以既能直接导入手机日历，也能再发到群里用 ``/导入课表`` 还原
                 给对应成员。
* ``backup`` —— 按 SQLite 里原本的存储结构导出：每位成员一个 JSON（含字段原始
                 值和事件原文），加上一份休假/调休标记。用于把课表完整还原到
                 同一个或另一个会话。

读取压缩包时只在内存里解压，不按条目名写磁盘，所以不存在 zip-slip；逐条检查
声明大小并累计解压总量，避免解压炸弹。这里只负责压缩包的格式，写入存储由
``course_schedule`` 负责。
"""

from __future__ import annotations

import io
import json
import re
import zipfile
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from typing import Any

from .constants import (
    DAY_OVERRIDE_HOLIDAY,
    DAY_OVERRIDE_SHIFT,
    MAX_ARCHIVE_BYTES,
    MAX_ARCHIVE_ENTRIES,
    MAX_ARCHIVE_UNPACKED_BYTES,
    MAX_ICS_BYTES,
)
from .domain import _display_name
from .ics import _serialize_schedule_ics

EXPORT_FORMAT_ICS = "ics"
EXPORT_FORMAT_BACKUP = "backup"

ICS_BUNDLE_FORMAT = "course-schedule-ics-bundle"
BACKUP_FORMAT = "course-schedule-backup"
ARCHIVE_VERSION = 1

MANIFEST_NAME = "manifest.json"
DAY_OVERRIDES_NAME = "day_overrides.json"
MEMBER_DIR = "members"
README_NAME = "README.txt"

# 群文件约定：``schedule<QQ号>.ics`` 指向该 QQ 号的课表。只有这种命名（以及
# 压缩包里由插件自己生成的同名文件）能确定成员，其它文件名一律忽略，避免把
# ``20260901.ics`` 这样的日期文件名误当成 QQ 号。
_MEMBER_FILE_RE = re.compile(r"schedule(\d+)\.ics", re.IGNORECASE)

_ICS_README = """本压缩包由 AstrBot 课程表插件导出，每位成员一个 .ics 文件。

- 文件名遵循 schedule<QQ号>.ics 约定：可以直接导入手机或电脑日历，也可以发到
  群里用 /导入课表 还原给对应成员。
- 文件名里的 QQ 号决定导入对象，因此为其他成员导入需要管理员权限。
- manifest.json 记录导出时间、来源会话和成员名单。
- 休假/调休标记不在 .ics 里；需要完整还原请改用「导出原始备份」。
"""

_BACKUP_README = """本压缩包是 AstrBot 课程表插件的原始数据备份，请不要手工修改。

- members/schedule<QQ号>.json  每位成员的存储数据（字段原值 + 事件原文，
  因此 RDATE/EXDATE 等未建模属性也能还原）
- day_overrides.json  休假/调休标记
- manifest.json  导出时间、来源会话和成员名单

在 WebUI 的「导入 / 导出」里导入本压缩包即可还原：备份里出现的成员会被覆盖为
备份时的课程和休假/调休标记，备份里没有的标记会被清除；成员昵称保持不变。
"""


def _member_id_from_filename(filename: str) -> str:
    """Return the QQ号 in the group-file convention ``schedule<QQ号>.ics``.

    Files named that way belong to the member whose number they carry, so
    referencing one addresses that member instead of the uploader.  Any other
    name yields an empty string, meaning "no member in the file name".
    """
    match = _MEMBER_FILE_RE.fullmatch(Path(str(filename or "").strip()).name)
    return match.group(1) if match else ""


def _member_file_stem(user_id: str) -> str:
    """File name (without extension) used for one member inside an archive."""
    raw = str(user_id or "").strip()
    if raw.isdigit():
        return f"schedule{raw}"
    safe = "".join(
        character if character.isalnum() or character in "._-" else "_"
        for character in raw
    )[:64]
    return safe.strip("._") or "member"


def _member_ics_filename(user_id: str) -> str:
    return f"{_member_file_stem(user_id)}.ics"


def _member_events(info: Any) -> list[dict[str, Any]]:
    if not isinstance(info, dict):
        return []
    raw = info.get("events")
    if not isinstance(raw, list):
        return []
    return [dict(item) for item in raw if isinstance(item, dict)]


def _scope_slug(scope_id: str) -> str:
    """Turn ``group:123456`` into a safe part of a download file name."""
    safe = "".join(
        character if character.isalnum() else "-"
        for character in str(scope_id or "").strip()
    ).strip("-")
    return safe or "scope"


def _member_ics_text(info: Any, name: str) -> str:
    """Serialize one member's stored schedule, labelled for calendar clients."""
    label = f"{name}的课表" if name else "课程表"
    return _serialize_schedule_ics(
        _member_events(info), str((info or {}).get("ics") or ""), label
    )


def _unique_entry_name(name: str, used: set[str]) -> str:
    """Keep two members with the same sanitized name from overwriting."""
    if name not in used:
        used.add(name)
        return name
    stem, dot, suffix = name.rpartition(".")
    index = 2
    while True:
        candidate = f"{stem or name}-{index}{dot}{suffix}" if dot else f"{name}-{index}"
        if candidate not in used:
            used.add(candidate)
            return candidate
        index += 1


def _dump_json(payload: Any) -> bytes:
    return json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")


def _manifest_payload(
    archive_format: str,
    scope_id: str,
    members: list[dict[str, Any]],
    event_count: int,
    stamp: datetime,
) -> bytes:
    return _dump_json(
        {
            "format": archive_format,
            "version": ARCHIVE_VERSION,
            "exported_at": stamp.strftime("%Y-%m-%d %H:%M:%S%z"),
            "scope_id": str(scope_id or ""),
            "member_count": len(members),
            "event_count": event_count,
            "members": members,
        }
    )


def _zip_bytes(entries: list[tuple[str, bytes]], stamp: datetime) -> bytes:
    """Pack the entries into a zip, stamped once so one export is one moment."""
    moment = (
        stamp.year,
        stamp.month,
        stamp.day,
        stamp.hour,
        stamp.minute,
        stamp.second,
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in entries:
            item = zipfile.ZipInfo(name, date_time=moment)
            item.compress_type = zipfile.ZIP_DEFLATED
            item.external_attr = 0o644 << 16
            archive.writestr(item, payload)
    return buffer.getvalue()


def _build_ics_bundle(
    scope_id: str, members: dict[str, Any], *, stamp: datetime
) -> bytes:
    """One ``schedule<QQ号>.ics`` per member, plus manifest and readme."""
    entries: list[tuple[str, bytes]] = []
    listed: list[dict[str, Any]] = []
    used: set[str] = set()
    event_total = 0
    for user_id, info in members.items():
        if not isinstance(info, dict):
            continue
        member_id = str(user_id)
        name = _display_name(info.get("name") or member_id)
        events = _member_events(info)
        filename = _unique_entry_name(_member_ics_filename(member_id), used)
        entries.append((filename, _member_ics_text(info, name).encode("utf-8")))
        listed.append(
            {
                "user_id": member_id,
                "name": name,
                "file": filename,
                "event_count": len(events),
            }
        )
        event_total += len(events)
    entries.append((README_NAME, _ICS_README.encode("utf-8")))
    entries.append(
        (
            MANIFEST_NAME,
            _manifest_payload(
                ICS_BUNDLE_FORMAT, scope_id, listed, event_total, stamp
            ),
        )
    )
    return _zip_bytes(entries, stamp)


def _build_backup_archive(
    scope_id: str,
    members: dict[str, Any],
    overrides: list[dict[str, Any]],
    *,
    stamp: datetime,
) -> bytes:
    """Members in their stored shape plus the scope's 休假/调休 markers."""
    entries: list[tuple[str, bytes]] = []
    listed: list[dict[str, Any]] = []
    used: set[str] = set()
    event_total = 0
    for user_id, info in members.items():
        if not isinstance(info, dict):
            continue
        member_id = str(user_id)
        name = _display_name(info.get("name") or member_id)
        events = _member_events(info)
        stored = {
            key: value
            for key, value in info.items()
            if key not in {"_revision", "_day_overrides", "events", "event_count"}
        }
        filename = _unique_entry_name(
            f"{MEMBER_DIR}/{_member_file_stem(member_id)}.json", used
        )
        entries.append(
            (
                filename,
                _dump_json(
                    {
                        "user_id": member_id,
                        "name": name,
                        "info": stored,
                        "events": events,
                    }
                ),
            )
        )
        listed.append(
            {
                "user_id": member_id,
                "name": name,
                "file": filename,
                "event_count": len(events),
            }
        )
        event_total += len(events)
    entries.append((DAY_OVERRIDES_NAME, _dump_json(_override_payloads(overrides))))
    entries.append((README_NAME, _BACKUP_README.encode("utf-8")))
    entries.append(
        (
            MANIFEST_NAME,
            _manifest_payload(BACKUP_FORMAT, scope_id, listed, event_total, stamp),
        )
    )
    return _zip_bytes(entries, stamp)


def _override_payloads(overrides: Any) -> list[dict[str, str]]:
    """Keep only the stored columns of each marker, in a stable key order."""
    rows: list[dict[str, str]] = []
    for row in overrides or []:
        if not isinstance(row, dict):
            continue
        rows.append(
            {
                "user_id": str(row.get("user_id") or ""),
                "day": str(row.get("day") or ""),
                "kind": str(row.get("kind") or ""),
                "source_day": str(row.get("source_day") or ""),
                "created_by": str(row.get("created_by") or ""),
                "created_at": str(row.get("created_at") or ""),
            }
        )
    return rows


def _decode_text(payload: bytes) -> str:
    """Decode one archive entry; the format is UTF-8 unless a tool said otherwise.

    iCalendar and JSON are UTF-8 by spec, but files exported by other tools are
    occasionally GBK.  Latin-1 never fails, so it is the last resort even though
    it garbles non-ASCII text instead of rejecting the file.
    """
    try:
        return payload.decode("utf-8-sig")
    except UnicodeDecodeError:
        with suppress(UnicodeDecodeError, LookupError):
            return payload.decode("gbk")
        return payload.decode("latin-1")


def _read_entries(archive: zipfile.ZipFile) -> dict[str, bytes]:
    """Read every file of the archive into memory, with size guards."""
    items = [item for item in archive.infolist() if not item.is_dir()]
    if len(items) > MAX_ARCHIVE_ENTRIES:
        raise ValueError(f"压缩包内文件过多（最多 {MAX_ARCHIVE_ENTRIES} 个）。")
    entries: dict[str, bytes] = {}
    unpacked = 0
    for item in items:
        unpacked += int(item.file_size)
        if unpacked > MAX_ARCHIVE_UNPACKED_BYTES:
            raise ValueError(
                f"压缩包解压后超过 {MAX_ARCHIVE_UNPACKED_BYTES // 1024 // 1024} MiB，未导入。"
            )
        name = str(item.filename).replace("\\", "/").lstrip("/")
        if not name:
            continue
        try:
            entries[name] = archive.read(item)
        except (zipfile.BadZipFile, RuntimeError, OSError) as exc:
            raise ValueError(f"压缩包已损坏（{name}）。") from exc
    return entries


def _load_manifest(entries: dict[str, bytes]) -> dict[str, Any]:
    payload = entries.get(MANIFEST_NAME)
    if payload is None:
        return {}
    try:
        manifest = json.loads(_decode_text(payload))
    except (ValueError, UnicodeDecodeError):
        return {}
    return manifest if isinstance(manifest, dict) else {}


def _manifest_members(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    rows = manifest.get("members")
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict)]


def _manifest_names(manifest: dict[str, Any]) -> dict[str, str]:
    """user_id -> display name, from the manifest's member list."""
    names: dict[str, str] = {}
    for row in _manifest_members(manifest):
        user_id = str(row.get("user_id") or "").strip()
        if user_id:
            names[user_id] = _display_name(row.get("name") or user_id)
    return names


def _parse_ics_bundle(
    entries: dict[str, bytes], manifest: dict[str, Any]
) -> dict[str, Any]:
    """Collect the .ics files as ``{user_id, name, content}`` members."""
    names = _manifest_names(manifest)
    by_file = {
        str(row.get("file") or "").replace("\\", "/").lstrip("/"): str(
            row.get("user_id") or ""
        ).strip()
        for row in _manifest_members(manifest)
    }
    members: list[dict[str, Any]] = []
    skipped: list[str] = []
    for name, payload in entries.items():
        if not name.lower().endswith(".ics"):
            continue
        user_id = _member_id_from_filename(name) or by_file.get(name, "")
        if not user_id:
            # A hand-made or renamed file: without a QQ号 there is no way to
            # tell whose schedule it is, so it is reported instead of guessed.
            skipped.append(f"{name}（文件名里没有 QQ 号）")
            continue
        if len(payload) > MAX_ICS_BYTES:
            skipped.append(f"{name}（超过 {MAX_ICS_BYTES // 1024 // 1024} MiB）")
            continue
        members.append(
            {
                "user_id": user_id,
                "name": names.get(user_id, ""),
                "file": name,
                "content": _decode_text(payload),
            }
        )
    return {
        "format": EXPORT_FORMAT_ICS,
        "scope_id": str(manifest.get("scope_id") or ""),
        "members": members,
        "day_overrides": [],
        "skipped": skipped,
    }


def _parse_day_overrides(entries: dict[str, bytes]) -> list[dict[str, str]]:
    payload = entries.get(DAY_OVERRIDES_NAME)
    if payload is None:
        return []
    try:
        rows = json.loads(_decode_text(payload))
    except (ValueError, UnicodeDecodeError):
        return []
    if not isinstance(rows, list):
        return []
    return [
        row
        for row in _override_payloads(rows)
        if row["user_id"]
        and row["day"]
        and row["kind"] in {DAY_OVERRIDE_HOLIDAY, DAY_OVERRIDE_SHIFT}
    ]


def _parse_backup_archive(
    entries: dict[str, bytes], manifest: dict[str, Any]
) -> dict[str, Any]:
    """Read the members exactly as they were stored, plus the day markers."""
    members: list[dict[str, Any]] = []
    skipped: list[str] = []
    for row in _manifest_members(manifest):
        user_id = str(row.get("user_id") or "").strip()
        filename = str(row.get("file") or "").replace("\\", "/").lstrip("/")
        payload = entries.get(filename)
        if payload is None and user_id:
            # Tolerate a hand-moved member file: the stem still names the QQ号.
            payload = entries.get(f"{MEMBER_DIR}/{_member_file_stem(user_id)}.json")
        if not user_id or payload is None:
            skipped.append(filename or user_id or "?")
            continue
        try:
            stored = json.loads(_decode_text(payload))
        except (ValueError, UnicodeDecodeError):
            skipped.append(f"{filename}（不是有效的 JSON）")
            continue
        if not isinstance(stored, dict):
            skipped.append(f"{filename}（不是有效的成员记录）")
            continue
        info = stored.get("info")
        members.append(
            {
                "user_id": user_id,
                "name": _display_name(
                    stored.get("name") or (info or {}).get("name") or user_id
                ),
                "file": filename,
                "info": info if isinstance(info, dict) else {},
                "events": _member_events(stored),
            }
        )
    if not members:
        raise ValueError("压缩包里没有可导入的成员记录。")
    return {
        "format": EXPORT_FORMAT_BACKUP,
        "scope_id": str(manifest.get("scope_id") or ""),
        "members": members,
        "day_overrides": _parse_day_overrides(entries),
        "skipped": skipped,
    }


def _read_archive(data: bytes) -> dict[str, Any]:
    """Parse one uploaded archive into members, day markers and notes.

    Raises ``ValueError`` with a message that can be shown to the user when the
    payload is not a zip this plugin knows how to import.
    """
    if not data:
        raise ValueError("上传的文件是空的。")
    if len(data) > MAX_ARCHIVE_BYTES:
        raise ValueError(
            f"压缩包超过 {MAX_ARCHIVE_BYTES // 1024 // 1024} MiB，未导入。"
        )
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = _read_entries(archive)
    except (zipfile.BadZipFile, OSError) as exc:
        raise ValueError("这不是有效的 .zip 压缩包，请选择插件导出的 .zip 文件。") from exc

    manifest = _load_manifest(entries)
    archive_format = str(manifest.get("format") or "").strip()
    if archive_format == BACKUP_FORMAT:
        return _parse_backup_archive(entries, manifest)
    if archive_format == ICS_BUNDLE_FORMAT or any(
        name.lower().endswith(".ics") for name in entries
    ):
        return _parse_ics_bundle(entries, manifest)
    raise ValueError("压缩包里没有 manifest.json，也没有 .ics 文件，无法导入。")
