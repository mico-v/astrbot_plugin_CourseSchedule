from __future__ import annotations

import asyncio
import re
import tempfile
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.request import urlopen
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw, ImageFont

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register


PLUGIN_ID = "astrbot_plugin_course_schedule"
STORE_KEY = "course_schedule:v1"
PLUGIN_DIR = Path(__file__).resolve().parent
FONT_DIR = PLUGIN_DIR / "assets" / "fonts"
ROOT_SCHEDULE_FILE_RE = re.compile(r"^schedule(\d+)\.ics$", re.IGNORECASE)
SCHEDULE_FOLDER_FILE_RE = re.compile(r"^(\d+)\.ics$", re.IGNORECASE)
SCHEDULE_FOLDER_NAME = "schedule"
MAX_ICS_BYTES = 2 * 1024 * 1024
MAX_EVENTS_PER_FILE = 120
LOCAL_TZ = ZoneInfo("Asia/Shanghai")
WEEKDAY_CODES = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _datetime_to_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def _parse_timestamp(value: Any) -> datetime | None:
    if value in (None, ""):
        return None

    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=LOCAL_TZ).astimezone(timezone.utc)
        return value.astimezone(timezone.utc)

    if isinstance(value, (int, float)):
        timestamp = float(value)
        if timestamp > 10_000_000_000:
            timestamp /= 1000
        try:
            return datetime.fromtimestamp(timestamp, timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None

    text = str(value).strip()
    if not text:
        return None

    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        try:
            return _parse_timestamp(float(text))
        except ValueError:
            return None

    normalized = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=LOCAL_TZ)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        pass

    for input_format in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            parsed = datetime.strptime(text, input_format)
            return parsed.replace(tzinfo=LOCAL_TZ).astimezone(timezone.utc)
        except ValueError:
            continue

    return None


def _timestamp_iso(value: Any | None = None) -> str:
    parsed = _parse_timestamp(value) if value is not None else None
    return _datetime_to_iso(parsed or datetime.now(timezone.utc))


def _empty_store() -> dict[str, Any]:
    return {"version": 1, "groups": {}}


def _normalize_store(raw_store: Any) -> dict[str, Any]:
    if not isinstance(raw_store, dict):
        return _empty_store()

    groups = raw_store.get("groups")
    if not isinstance(groups, dict):
        raw_store["groups"] = {}

    raw_store.setdefault("version", 1)
    return raw_store


def _scope_id(event: AstrMessageEvent) -> str:
    group_id = event.get_group_id()
    if group_id:
        return f"group:{group_id}"

    return f"private:{event.get_sender_id()}"


def _scope_label(event: AstrMessageEvent) -> str:
    group_id = event.get_group_id()
    return f"群 {group_id}" if group_id else "私聊"


def _format_time(value: str | None) -> str:
    if not value:
        return "未知时间"

    return value.replace("T", " ").replace("+00:00", " UTC")


def _group_file_updated_at(file_info: dict[str, Any]) -> datetime | None:
    timestamp_keys = (
        "modify_time",
        "modified_time",
        "update_time",
        "updated_at",
        "mtime",
        "upload_time",
        "created_at",
        "create_time",
        "ctime",
        "time",
    )
    for key in timestamp_keys:
        parsed = _parse_timestamp(file_info.get(key))
        if parsed:
            return parsed

    return None


def _local_schedule_updated_at(info: dict[str, Any]) -> datetime | None:
    return (
        _parse_timestamp(info.get("schedule_updated_at"))
        or _parse_timestamp(info.get("content_updated_at"))
        or _parse_timestamp(info.get("updated_at"))
    )


def _compare_timestamps(left: datetime | None, right: datetime | None) -> int | None:
    if not left or not right:
        return None

    delta = (left - right).total_seconds()
    if abs(delta) <= 2:
        return 0
    return 1 if delta > 0 else -1


def _unfold_ics_lines(content: str) -> list[str]:
    lines: list[str] = []
    for raw_line in content.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw_line.startswith((" ", "\t")) and lines:
            lines[-1] += raw_line[1:]
        else:
            lines.append(raw_line)

    return lines


def _decode_ics_text(value: str) -> str:
    return (
        value.replace("\\n", "\n")
        .replace("\\N", "\n")
        .replace("\\,", ",")
        .replace("\\;", ";")
        .replace("\\\\", "\\")
        .strip()
    )


def _parse_ics_datetime(value: str) -> str:
    raw = value.strip()
    if not raw:
        return ""

    formats = [
        ("%Y%m%dT%H%M%SZ", "%Y-%m-%d %H:%M UTC"),
        ("%Y%m%dT%H%M%S", "%Y-%m-%d %H:%M"),
        ("%Y%m%dT%H%M", "%Y-%m-%d %H:%M"),
        ("%Y%m%d", "%Y-%m-%d"),
    ]
    for input_format, output_format in formats:
        try:
            return datetime.strptime(raw, input_format).strftime(output_format)
        except ValueError:
            continue

    return raw


def _parse_ics_datetime_obj(value: str, tzid: str | None = None) -> datetime | None:
    raw = value.strip()
    if not raw:
        return None

    tz = LOCAL_TZ
    if tzid:
        try:
            tz = ZoneInfo(tzid)
        except Exception:
            tz = LOCAL_TZ

    formats = [
        ("%Y%m%dT%H%M%SZ", timezone.utc),
        ("%Y%m%dT%H%M%S", tz),
        ("%Y%m%dT%H%M", tz),
        ("%Y%m%d", tz),
    ]
    for input_format, timezone_info in formats:
        try:
            parsed = datetime.strptime(raw, input_format)
            return parsed.replace(tzinfo=timezone_info).astimezone(LOCAL_TZ)
        except ValueError:
            continue

    return None


def _parse_rrule(value: str) -> str:
    if not value:
        return ""

    parts: dict[str, str] = {}
    for item in value.split(";"):
        key, _, item_value = item.partition("=")
        if key and item_value:
            parts[key.upper()] = item_value

    freq_map = {
        "DAILY": "每天",
        "WEEKLY": "每周",
        "MONTHLY": "每月",
        "YEARLY": "每年",
    }
    text = freq_map.get(parts.get("FREQ", ""), parts.get("FREQ", ""))
    if parts.get("BYDAY"):
        text += f" {parts['BYDAY']}"
    if parts.get("COUNT"):
        text += f" 共 {parts['COUNT']} 次"
    if parts.get("UNTIL"):
        text += f" 至 {_parse_ics_datetime(parts['UNTIL'])}"

    return text.strip()


def _parse_rrule_parts(value: str) -> dict[str, str]:
    parts: dict[str, str] = {}
    for item in value.split(";"):
        key, _, item_value = item.partition("=")
        if key and item_value:
            parts[key.upper()] = item_value
    return parts


def _parse_ics_key(key: str) -> tuple[str, dict[str, str]]:
    parts = key.split(";")
    name = parts[0].upper()
    params: dict[str, str] = {}
    for item in parts[1:]:
        param_name, _, param_value = item.partition("=")
        if param_name and param_value:
            params[param_name.upper()] = param_value
    return name, params


def _parse_ics_events(content: str) -> list[dict[str, str]]:
    events: list[dict[str, str]] = []
    current: dict[str, str] | None = None

    for line in _unfold_ics_lines(content):
        if not line:
            continue

        upper_line = line.upper()
        if upper_line == "BEGIN:VEVENT":
            current = {}
            continue

        if upper_line == "END:VEVENT":
            if current:
                events.append(current)
            current = None
            continue

        if current is None or ":" not in line:
            continue

        key, value = line.split(":", 1)
        name, params = _parse_ics_key(key)
        if name in {"SUMMARY", "LOCATION", "DESCRIPTION"}:
            current[name] = _decode_ics_text(value)
        elif name in {"DTSTART", "DTEND", "RRULE"}:
            current[name] = value.strip()
            if params.get("TZID"):
                current[f"{name}_TZID"] = params["TZID"]

    events.sort(key=lambda event: event.get("DTSTART", ""))
    return events[:MAX_EVENTS_PER_FILE]


def _format_ics_schedule(events: list[dict[str, str]]) -> str:
    if not events:
        return "未解析到课程事件。"

    lines: list[str] = []
    for index, event in enumerate(events, start=1):
        summary = event.get("SUMMARY") or "未命名课程"
        start = _parse_ics_datetime(event.get("DTSTART", ""))
        end = _parse_ics_datetime(event.get("DTEND", ""))
        location = event.get("LOCATION")
        rrule = _parse_rrule(event.get("RRULE", ""))

        time_text = start
        if end:
            time_text = f"{start} - {end}" if start else end

        line = f"{index}. {summary}"
        if time_text:
            line += f" | {time_text}"
        if rrule:
            line += f" | {rrule}"
        if location:
            line += f" | {location}"
        lines.append(line)

    return "\n".join(lines)


def _parse_schedule_ics(content: str) -> tuple[list[dict[str, str]], str]:
    events = _parse_ics_events(content)
    return events, _format_ics_schedule(events)


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


def _event_datetimes(event: dict[str, str]) -> tuple[datetime | None, datetime | None]:
    start = _parse_ics_datetime_obj(event.get("DTSTART", ""), event.get("DTSTART_TZID"))
    end = _parse_ics_datetime_obj(event.get("DTEND", ""), event.get("DTEND_TZID"))
    if start and not end:
        end = start + timedelta(hours=1, minutes=30)
    if start and end and end <= start:
        end = start + timedelta(hours=1, minutes=30)
    return start, end


def _copy_occurrence(event: dict[str, str], start: datetime, end: datetime) -> dict[str, Any]:
    copied: dict[str, Any] = dict(event)
    copied["_start"] = start
    copied["_end"] = end
    return copied


def _expand_event_occurrences(
    event: dict[str, str], start_bound: datetime, end_bound: datetime
) -> list[dict[str, Any]]:
    start, end = _event_datetimes(event)
    if not start or not end:
        return []

    duration = end - start
    rrule = event.get("RRULE", "")
    if not rrule:
        if start < end_bound and end > start_bound:
            return [_copy_occurrence(event, start, end)]
        return []

    parts = _parse_rrule_parts(rrule)
    if parts.get("FREQ") != "WEEKLY":
        if start < end_bound and end > start_bound:
            return [_copy_occurrence(event, start, end)]
        return []

    weekdays = [start.weekday()]
    if parts.get("BYDAY"):
        weekdays = [
            WEEKDAY_CODES.index(code)
            for code in parts["BYDAY"].split(",")
            if code in WEEKDAY_CODES
        ] or weekdays

    until = _parse_ics_datetime_obj(parts.get("UNTIL", ""), event.get("DTSTART_TZID"))
    count = int(parts.get("COUNT") or 0)
    interval = max(int(parts.get("INTERVAL") or 1), 1)
    occurrences: list[dict[str, Any]] = []
    generated = 0

    cursor_date = start_bound.date() - timedelta(days=7 * interval)
    last_date = end_bound.date() + timedelta(days=7)
    while cursor_date <= last_date:
        if cursor_date.weekday() in weekdays:
            occurrence_start = datetime.combine(
                cursor_date, start.timetz().replace(tzinfo=None), tzinfo=LOCAL_TZ
            )
            weeks_from_start = (occurrence_start.date() - start.date()).days // 7
            if occurrence_start >= start and weeks_from_start % interval == 0:
                generated += 1
                occurrence_end = occurrence_start + duration
                if count and generated > count:
                    break
                if until and occurrence_start > until:
                    break
                if occurrence_start < end_bound and occurrence_end > start_bound:
                    occurrences.append(_copy_occurrence(event, occurrence_start, occurrence_end))
        cursor_date += timedelta(days=1)

    occurrences.sort(key=lambda item: item["_start"])
    return occurrences


def _expand_member_occurrences(
    member_info: dict[str, Any], start_bound: datetime, end_bound: datetime
) -> list[dict[str, Any]]:
    events = member_info.get("events")
    if not isinstance(events, list):
        return []

    occurrences: list[dict[str, Any]] = []
    for event in events:
        if isinstance(event, dict):
            occurrences.extend(_expand_event_occurrences(event, start_bound, end_bound))

    occurrences.sort(key=lambda item: item["_start"])
    return occurrences


def _day_bounds(target_date: date) -> tuple[datetime, datetime]:
    start = datetime.combine(target_date, time.min, tzinfo=LOCAL_TZ)
    return start, start + timedelta(days=1)


def _week_bounds(today: date) -> tuple[datetime, datetime]:
    start_date = today - timedelta(days=today.weekday())
    start = datetime.combine(start_date, time.min, tzinfo=LOCAL_TZ)
    return start, start + timedelta(days=7)


def _format_occurrence_line(occurrence: dict[str, Any]) -> str:
    start: datetime = occurrence["_start"]
    end: datetime = occurrence["_end"]
    summary = occurrence.get("SUMMARY") or "未命名课程"
    location = occurrence.get("LOCATION")
    text = f"{start:%H:%M}-{end:%H:%M} {summary}"
    if location:
        text += f" @ {location}"
    return text


def _current_or_next(occurrences: list[dict[str, Any]], now: datetime) -> tuple[str, dict[str, Any] | None]:
    for occurrence in occurrences:
        if occurrence["_start"] <= now < occurrence["_end"]:
            return "正在上", occurrence
        if occurrence["_start"] > now:
            return "下一节", occurrence
    return "无课", None


def _duration_hours(occurrences: list[dict[str, Any]]) -> float:
    seconds = sum((item["_end"] - item["_start"]).total_seconds() for item in occurrences)
    return seconds / 3600


def _is_own_query(query: str) -> bool:
    return str(query or "").strip().lower() in {"", "我", "自己", "本人", "me", "self"}


def _parse_date_query(day: str) -> tuple[date | None, str]:
    normalized = str(day or "").strip().lower()
    today = datetime.now(LOCAL_TZ).date()
    if normalized in {"", "today", "今天", "今日"}:
        return today, "今天"
    if normalized in {"tomorrow", "明天", "明日"}:
        return today + timedelta(days=1), "明天"

    try:
        parsed = date.fromisoformat(normalized)
        return parsed, f"{parsed:%Y-%m-%d}"
    except ValueError:
        return None, ""


def _strip_command(message_str: str, commands: set[str]) -> str:
    text = str(message_str or "").strip()
    if not text:
        return ""

    if text[0] in {"/", "／"}:
        text = text[1:].lstrip()

    for command in sorted(commands, key=len, reverse=True):
        if text == command:
            return ""
        if text.startswith(f"{command} "):
            return text[len(command) :].strip()

    return text


def _help_text() -> str:
    return "\n".join(
        [
            "课程表插件命令：",
            "/课程表 同步群文件 - 群文件与本地 .ics 双向同步，较新的版本覆盖较旧版本",
            "/课程表 导出 - 将自己的原始 .ics 上传到当前群",
            "/课程表 查看 [昵称或QQ号] - 查看自己或群友的完整课程表",
            "/课程表 列表 - 列出当前会话已保存课程表成员",
            "/课程表 保存 <课程表内容> - 手动保存文本课程表",
            "/课程表 删除 - 删除自己当前会话下的课程表",
            "/查看课表 - 快速查看自己完整课程表",
            "/查看明日课表 - 查看自己明天课程",
            "/群友在上什么课 - 群友当前或下一节课图片",
            "/群友明天上什么课 - 群友明日课程图片",
            "/本周上课排行 - 本周群友上课时长排行图片",
            "",
            "群文件支持两种放法：",
            "1. 根目录 schedule123456.ics",
            "2. schedule/123456.ics",
            "其中 123456 是 QQ 号。",
            "",
            "兼容短命令：",
            "/课程表同步群文件",
            "/课程表导出",
            "/课程表查看",
            "/课程表列表",
            "/课程表保存",
            "/课程表删除",
            "",
            "示例：",
            "/课程表 同步群文件",
            "/群友在上什么课",
        ]
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
    filename = f"schedule{user_id}.ics"
    return filename, _write_temp_ics(filename, content)


def _asset_temp_path(filename: str) -> str:
    temp_dir = Path(tempfile.gettempdir()) / PLUGIN_ID
    temp_dir.mkdir(parents=True, exist_ok=True)
    return str(temp_dir / filename)


def _load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = [
        str(FONT_DIR / "NotoSansCJKsc-Bold.otf") if bold else "",
        str(FONT_DIR / "NotoSansCJKsc-Regular.otf"),
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc" if bold else "",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return ImageFont.truetype(candidate, size)
    return ImageFont.load_default()


def _ellipsis(text: str, font: ImageFont.ImageFont, max_width: int) -> str:
    if font.getlength(text) <= max_width:
        return text

    suffix = "..."
    while text and font.getlength(text + suffix) > max_width:
        text = text[:-1]
    return text + suffix if text else suffix


def _fetch_avatar(user_id: str, size: int) -> Image.Image:
    url = f"https://q1.qlogo.cn/g?b=qq&nk={user_id}&s=100"
    try:
        with urlopen(url, timeout=5) as response:
            avatar = Image.open(response).convert("RGB").resize((size, size))
    except Exception:
        avatar = Image.new("RGB", (size, size), "#d8dee9")
        draw = ImageDraw.Draw(avatar)
        font = _load_font(20, bold=True)
        label = user_id[-2:] if user_id else "?"
        draw.text((size / 2, size / 2), label, fill="#3b4252", font=font, anchor="mm")

    mask = Image.new("L", (size, size), 0)
    draw = ImageDraw.Draw(mask)
    draw.ellipse((0, 0, size - 1, size - 1), fill=255)
    rounded = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    rounded.paste(avatar, (0, 0), mask)
    return rounded


def _draw_rows_image(title: str, rows: list[dict[str, str]], filename: str) -> str:
    width = 1100
    row_height = 104
    header_height = 116
    footer_height = 28
    height = max(260, header_height + row_height * max(len(rows), 1) + footer_height)
    image = Image.new("RGB", (width, height), "#f5f7fb")
    draw = ImageDraw.Draw(image)
    title_font = _load_font(36, bold=True)
    name_font = _load_font(24, bold=True)
    body_font = _load_font(22)
    small_font = _load_font(18)

    draw.rectangle((0, 0, width, 92), fill="#263238")
    draw.text((36, 46), title, fill="#ffffff", font=title_font, anchor="lm")

    if not rows:
        draw.text((width / 2, height / 2), "暂无课程数据", fill="#607d8b", font=body_font, anchor="mm")
    for index, row in enumerate(rows):
        top = header_height + index * row_height
        left = 30
        right = width - 30
        fill = "#ffffff" if index % 2 == 0 else "#eef3f8"
        draw.rounded_rectangle((left, top, right, top + 86), radius=10, fill=fill)

        avatar = _fetch_avatar(row["user_id"], 58)
        image.paste(avatar, (left + 20, top + 14), avatar)
        draw.text((left + 92, top + 26), _ellipsis(row["name"], name_font, 240), fill="#263238", font=name_font)
        draw.text((left + 92, top + 58), row["subtitle"], fill="#607d8b", font=small_font)

        status_color = "#2e7d32" if row.get("status") == "正在上" else "#1565c0"
        draw.text((left + 365, top + 24), row.get("status", ""), fill=status_color, font=name_font)
        draw.text(
            (left + 470, top + 24),
            _ellipsis(row["course"], body_font, 510),
            fill="#263238",
            font=body_font,
        )
        draw.text((left + 470, top + 58), row["time"], fill="#455a64", font=small_font)

    path = _asset_temp_path(filename)
    image.save(path)
    return path


@register(PLUGIN_ID, "CourseSchedule", "保存并查询群友课程表", "0.2.0")
class CourseSchedulePlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)

    async def _load_store(self) -> dict[str, Any]:
        return _normalize_store(await self.get_kv_data(STORE_KEY, _empty_store()))

    async def _save_store(self, store: dict[str, Any]) -> None:
        await self.put_kv_data(STORE_KEY, store)

    async def _get_scope_members(
        self, event: AstrMessageEvent, create: bool = False
    ) -> dict[str, Any]:
        store = await self._load_store()
        groups = store["groups"]
        scope = _scope_id(event)

        if create:
            scope_data = groups.setdefault(scope, {"members": {}})
        else:
            scope_data = groups.get(scope, {"members": {}})

        members = scope_data.get("members")
        if not isinstance(members, dict):
            members = {}
            scope_data["members"] = members

        if create:
            await self._save_store(store)

        return members

    async def _upsert_ics_schedule(
        self,
        event: AstrMessageEvent,
        user_id: str,
        filename: str,
        ics_content: str,
        uploader_id: str | None = None,
        name: str | None = None,
        remote_updated_at: Any | None = None,
    ) -> None:
        events, schedule_text = _parse_schedule_ics(ics_content)
        now = _now_iso()
        schedule_updated_at = _timestamp_iso(remote_updated_at)
        store = await self._load_store()
        scope = _scope_id(event)
        scope_data = store["groups"].setdefault(scope, {"members": {}})
        members = scope_data.setdefault("members", {})
        previous = members.get(user_id, {})

        members[user_id] = {
            "name": name or previous.get("name") or user_id,
            "schedule": schedule_text,
            "updated_at": now,
            "schedule_updated_at": schedule_updated_at,
            "remote_updated_at": schedule_updated_at,
            "last_synced_at": now,
            "source": "ics",
            "source_file": filename,
            "uploader_id": uploader_id or "",
            "event_count": len(events),
            "events": events,
            "ics": ics_content,
        }
        await self._save_store(store)

    async def _mark_schedule_synced(
        self,
        event: AstrMessageEvent,
        user_id: str,
        source_file: str,
        remote_updated_at: Any | None = None,
    ) -> None:
        store = await self._load_store()
        scope_data = store["groups"].get(_scope_id(event))
        if not isinstance(scope_data, dict):
            return

        members = scope_data.get("members")
        if not isinstance(members, dict):
            return

        info = members.get(user_id)
        if not isinstance(info, dict):
            return

        now = _now_iso()
        synced_at = _timestamp_iso(remote_updated_at)
        info["source_file"] = source_file
        info["schedule_updated_at"] = synced_at
        info["remote_updated_at"] = synced_at
        info["last_synced_at"] = now
        info["updated_at"] = now
        await self._save_store(store)

    async def _resolve_member_info(
        self, event: AstrMessageEvent, query: str = ""
    ) -> tuple[str | None, dict[str, Any] | None, str | None]:
        members = await self._get_scope_members(event)
        if not members:
            return None, None, "当前会话还没有保存任何课程表。"

        normalized_query = str(query or "").strip()
        target_id = event.get_sender_id()

        if not _is_own_query(normalized_query):
            matched_ids = [
                user_id
                for user_id, info in members.items()
                if isinstance(info, dict)
                and (
                    normalized_query == user_id
                    or normalized_query in str(info.get("name", ""))
                )
            ]
            if not matched_ids:
                return None, None, f"没有找到“{normalized_query}”的课程表。"

            if len(matched_ids) > 1:
                names = [
                    f"{members[user_id].get('name') or user_id}({user_id})"
                    for user_id in matched_ids[:10]
                    if isinstance(members.get(user_id), dict)
                ]
                return None, None, "找到多个匹配成员，请用 QQ 号精确查询：\n" + "\n".join(names)

            target_id = matched_ids[0]

        info = members.get(target_id)
        if not isinstance(info, dict):
            if normalized_query and not _is_own_query(normalized_query):
                return None, None, f"没有找到“{normalized_query}”的课程表。"

            return None, None, "你还没有保存课程表，请先上传 .ics 并使用 /课程表 同步群文件。"

        return target_id, info, None

    async def _show_schedule_text(self, event: AstrMessageEvent, query: str = "") -> str:
        target_id, info, error = await self._resolve_member_info(event, query)
        if error:
            return error

        name = info.get("name") or target_id
        schedule = info.get("schedule") or "空"
        updated_at = _format_time(info.get("updated_at"))
        return f"{name} 的课程表：\n{schedule}\n\n更新时间：{updated_at}"

    async def _list_schedules_text(self, event: AstrMessageEvent) -> str:
        members = await self._get_scope_members(event)
        if not members:
            return "当前会话还没有保存任何课程表。"

        lines = [
            f"{info.get('name') or user_id}({user_id})"
            for user_id, info in members.items()
            if isinstance(info, dict)
        ]
        return f"{_scope_label(event)}已保存 {len(lines)} 份课程表：\n" + "\n".join(lines)

    async def _save_manual_schedule_text(
        self, event: AstrMessageEvent, schedule_text: str
    ) -> str:
        content = str(schedule_text or "").strip()
        if not content:
            return "请提供要保存的课程表内容，例如：/课程表 保存 周一 08:00 高数。"

        now = _now_iso()
        user_id = event.get_sender_id()
        store = await self._load_store()
        scope = _scope_id(event)
        scope_data = store["groups"].setdefault(scope, {"members": {}})
        members = scope_data.setdefault("members", {})
        previous = members.get(user_id, {})
        members[user_id] = {
            "name": event.get_sender_name() or previous.get("name") or user_id,
            "schedule": content,
            "updated_at": now,
            "content_updated_at": now,
            "last_synced_at": now,
            "source": "manual",
            "source_file": "",
            "uploader_id": user_id,
            "event_count": 0,
            "events": [],
            "ics": "",
        }
        await self._save_store(store)
        return "已保存文本课程表。"

    async def _delete_own_schedule_text(self, event: AstrMessageEvent) -> str:
        store = await self._load_store()
        scope = _scope_id(event)
        scope_data = store["groups"].get(scope)
        if not isinstance(scope_data, dict):
            return "你还没有保存课程表。"

        members = scope_data.get("members")
        if not isinstance(members, dict):
            return "你还没有保存课程表。"

        removed = members.pop(event.get_sender_id(), None)
        if removed is None:
            return "你还没有保存课程表。"

        await self._save_store(store)
        return "已删除你在当前会话中的课程表。"

    def _is_onebot_event(self, event: AstrMessageEvent) -> bool:
        return event.get_platform_name() == "aiocqhttp" and hasattr(event, "bot")

    async def _call_onebot_api(self, event: AstrMessageEvent, action: str, **params):
        if not self._is_onebot_event(event):
            raise RuntimeError("当前平台不是 aiocqhttp/OneBot v11，不能调用群文件 API。")

        bot = event.bot
        api = getattr(bot, "api", None)
        if api is None or not hasattr(api, "call_action"):
            raise RuntimeError("当前 OneBot 适配器没有暴露 call_action。")

        return await api.call_action(action, **params)

    async def _get_group_member_name(
        self, event: AstrMessageEvent, group_id: str, user_id: str
    ) -> str:
        try:
            data = self._extract_data(
                await self._call_onebot_api(
                    event,
                    "get_group_member_info",
                    group_id=int(group_id),
                    user_id=int(user_id),
                    no_cache=True,
                )
            )
            if isinstance(data, dict):
                return str(data.get("card") or data.get("nickname") or user_id)
        except Exception:
            pass
        return user_id

    async def _get_group_member_names(
        self, event: AstrMessageEvent, members: dict[str, Any]
    ) -> dict[str, str]:
        group_id = event.get_group_id()
        if not group_id:
            return {
                user_id: str(info.get("name") or user_id)
                for user_id, info in members.items()
                if isinstance(info, dict)
            }

        async def resolve(user_id: str, info: dict[str, Any]) -> tuple[str, str]:
            fetched_name = await self._get_group_member_name(event, group_id, user_id)
            if fetched_name and fetched_name != user_id:
                return user_id, fetched_name
            return user_id, str(info.get("name") or user_id)

        tasks = [
            resolve(user_id, info)
            for user_id, info in members.items()
            if isinstance(info, dict)
        ]
        if not tasks:
            return {}
        return dict(await asyncio.gather(*tasks))

    def _extract_data(self, response: Any) -> Any:
        if isinstance(response, dict) and "data" in response:
            return response["data"]

        return response

    def _extract_file_id(self, file_info: dict[str, Any]) -> str:
        return str(file_info.get("file_id") or file_info.get("id") or "")

    def _extract_folder_id(self, folder_info: dict[str, Any]) -> str:
        return str(folder_info.get("folder_id") or folder_info.get("id") or "")

    async def _list_group_files(
        self, event: AstrMessageEvent, group_id: str
    ) -> list[dict[str, Any]]:
        root = self._extract_data(
            await self._call_onebot_api(
                event, "get_group_root_files", group_id=int(group_id)
            )
        )
        files: list[dict[str, Any]] = []
        for file_info in root.get("files") or []:
            copied = dict(file_info)
            copied["_folder_path"] = ""
            files.append(copied)

        folders: list[tuple[dict[str, Any], str]] = [
            (folder, _folder_name(folder)) for folder in root.get("folders") or []
        ]
        seen_folder_ids: set[str] = set()

        while folders:
            folder, folder_path = folders.pop(0)
            folder_id = self._extract_folder_id(folder)
            if not folder_id or folder_id in seen_folder_ids:
                continue
            seen_folder_ids.add(folder_id)

            child = self._extract_data(
                await self._call_onebot_api(
                    event,
                    "get_group_files_by_folder",
                    group_id=int(group_id),
                    folder_id=folder_id,
                )
            )
            for file_info in child.get("files") or []:
                copied = dict(file_info)
                copied["_folder_path"] = folder_path
                copied["_folder_id"] = folder_id
                files.append(copied)

            for child_folder in child.get("folders") or []:
                child_name = _folder_name(child_folder)
                child_path = _join_folder_path(folder_path, child_name)
                folders.append((child_folder, child_path))

        return files

    async def _get_group_file_url(
        self, event: AstrMessageEvent, group_id: str, file_info: dict[str, Any]
    ) -> str:
        file_id = self._extract_file_id(file_info)
        busid = int(file_info.get("busid") or file_info.get("bus_id") or 0)
        if not file_id:
            raise ValueError("群文件缺少 file_id/id")

        response = self._extract_data(
            await self._call_onebot_api(
                event,
                "get_group_file_url",
                group_id=int(group_id),
                file_id=file_id,
                busid=busid,
            )
        )
        if isinstance(response, dict):
            url = response.get("url")
        else:
            url = None

        if not url:
            raise ValueError("协议端没有返回群文件下载链接")

        return str(url)

    async def _upload_schedule_file(
        self, event: AstrMessageEvent, group_id: str, user_id: str, ics_content: str
    ) -> str:
        filename, local_path = await asyncio.to_thread(
            _write_temp_schedule_ics, user_id, ics_content
        )
        await self._call_onebot_api(
            event,
            "upload_group_file",
            group_id=int(group_id),
            file=local_path,
            name=filename,
        )
        return filename

    async def _download_group_schedule(
        self,
        event: AstrMessageEvent,
        group_id: str,
        user_id: str,
        file_info: dict[str, Any],
        remote_updated_at: datetime | None,
    ) -> None:
        url = await self._get_group_file_url(event, group_id, file_info)
        ics_content = await asyncio.to_thread(_download_text, url)
        member_name = await self._get_group_member_name(event, group_id, user_id)
        await self._upsert_ics_schedule(
            event,
            user_id=user_id,
            filename=_display_group_file_path(file_info),
            ics_content=ics_content,
            uploader_id=str(file_info.get("uploader") or file_info.get("user_id") or ""),
            name=member_name,
            remote_updated_at=remote_updated_at,
        )

    async def _sync_group_files_text(self, event: AstrMessageEvent) -> str:
        group_id = event.get_group_id()
        if not group_id:
            return "只能在群聊中同步群文件。"

        try:
            files = await self._list_group_files(event, group_id)
        except Exception as exc:
            logger.error(f"Failed to list group files: {exc}")
            return f"读取群文件失败：{exc}"

        matched = []
        for file_info in files:
            filename = _file_name(file_info)
            folder_path = str(file_info.get("_folder_path") or "")
            if _extract_schedule_user_id(filename, folder_path):
                matched.append(file_info)

        members = await self._get_scope_members(event)
        remote_by_user: dict[str, dict[str, Any]] = {}
        for file_info in matched:
            user_id = _extract_schedule_user_id(
                _file_name(file_info), str(file_info.get("_folder_path") or "")
            )
            if not user_id:
                continue

            current = remote_by_user.get(user_id)
            if not current:
                remote_by_user[user_id] = file_info
                continue

            current_time = _group_file_updated_at(current)
            next_time = _group_file_updated_at(file_info)
            if _compare_timestamps(next_time, current_time) == 1 or (
                next_time and not current_time
            ):
                remote_by_user[user_id] = file_info

        downloaded: list[str] = []
        uploaded: list[str] = []
        skipped: list[str] = []
        failed: list[str] = []
        processed_users: set[str] = set()

        for user_id, file_info in remote_by_user.items():
            display_path = _display_group_file_path(file_info)
            processed_users.add(user_id)
            remote_updated_at = _group_file_updated_at(file_info)
            local_info = members.get(user_id)
            local_updated_at = (
                _local_schedule_updated_at(local_info)
                if isinstance(local_info, dict)
                else None
            )
            comparison = _compare_timestamps(local_updated_at, remote_updated_at)

            try:
                if (
                    comparison == 1
                    and isinstance(local_info, dict)
                    and local_info.get("ics")
                ):
                    uploaded_name = await self._upload_schedule_file(
                        event, group_id, user_id, str(local_info["ics"])
                    )
                    await self._mark_schedule_synced(
                        event, user_id, uploaded_name, datetime.now(timezone.utc)
                    )
                    uploaded.append(f"{user_id} -> {uploaded_name}")
                elif comparison == 0 and isinstance(local_info, dict):
                    await self._mark_schedule_synced(
                        event, user_id, display_path, remote_updated_at
                    )
                    skipped.append(f"{display_path} 已是最新")
                else:
                    await self._download_group_schedule(
                        event, group_id, user_id, file_info, remote_updated_at
                    )
                    downloaded.append(f"{display_path} -> {user_id}")
            except Exception as exc:
                logger.error(f"Failed to sync group file {display_path}: {exc}")
                failed.append(f"{display_path}: {exc}")

        for user_id, local_info in members.items():
            if user_id in processed_users or not isinstance(local_info, dict):
                continue
            ics_content = local_info.get("ics")
            if not ics_content:
                continue

            try:
                uploaded_name = await self._upload_schedule_file(
                    event, group_id, user_id, str(ics_content)
                )
                await self._mark_schedule_synced(
                    event, user_id, uploaded_name, datetime.now(timezone.utc)
                )
                uploaded.append(f"{user_id} -> {uploaded_name}")
            except Exception as exc:
                logger.error(f"Failed to upload local schedule for {user_id}: {exc}")
                failed.append(f"{user_id}: {exc}")

        lines = [
            "群文件双向同步完成："
            f"下载 {len(downloaded)} 个，上传 {len(uploaded)} 个，"
            f"跳过 {len(skipped)} 个，失败 {len(failed)} 个。"
        ]
        if downloaded:
            lines.append("已下载：")
            lines.extend(downloaded[:20])
        if uploaded:
            lines.append("已上传：")
            lines.extend(uploaded[:20])
        if skipped:
            lines.append("已跳过：")
            lines.extend(skipped[:10])
        if failed:
            lines.append("失败：")
            lines.extend(failed[:10])

        return "\n".join(lines)

    async def _export_own_ics_text(self, event: AstrMessageEvent) -> str:
        group_id = event.get_group_id()
        if not group_id:
            return "只能在群聊中上传课程表文件。"

        target_id, info, error = await self._resolve_member_info(event)
        if error:
            return "你还没有保存课程表。请先上传 schedule<QQ号>.ics 并同步。"

        ics_content = info.get("ics")
        if not ics_content:
            return "当前课程表不是从 .ics 导入的，无法导出原始 .ics。"

        filename = f"schedule{target_id or event.get_sender_id()}.ics"
        try:
            filename = await self._upload_schedule_file(
                event, group_id, target_id or event.get_sender_id(), str(ics_content)
            )
            await self._mark_schedule_synced(
                event,
                target_id or event.get_sender_id(),
                filename,
                datetime.now(timezone.utc),
            )
        except Exception as exc:
            logger.error(f"Failed to upload group file {filename}: {exc}")
            return f"上传群文件失败：{exc}"

        return f"已上传 {filename} 到当前群文件。"

    async def _member_day_schedule_text(
        self, event: AstrMessageEvent, query: str, day: str
    ) -> str:
        target_date, title = _parse_date_query(day)
        if not target_date:
            return "无法识别日期，请使用 today、tomorrow、今天、明天或 YYYY-MM-DD。"

        target_id, info, error = await self._resolve_member_info(event, query)
        if error:
            return error

        start_bound, end_bound = _day_bounds(target_date)
        occurrences = _expand_member_occurrences(info, start_bound, end_bound)

        name = info.get("name") or target_id
        if occurrences:
            lines = [f"{name} {title}课程："]
            lines.extend(_format_occurrence_line(item) for item in occurrences)
            return "\n".join(lines)

        if info.get("events"):
            return f"{name} {title}没有接下来要上的课程。"

        schedule = info.get("schedule") or "空"
        return (
            f"{name} 的课程表不是结构化 .ics 数据，无法按日期展开。"
            f"\n已保存内容：\n{schedule}"
        )

    async def _group_current_text(self, event: AstrMessageEvent) -> str:
        members = await self._get_scope_members(event)
        if not members:
            return "当前会话还没有保存任何课程表。"

        now = datetime.now(LOCAL_TZ)
        start_bound, end_bound = _day_bounds(now.date())
        names = await self._get_group_member_names(event, members)
        rows: list[tuple[str, str, str, str]] = []
        for user_id, info in members.items():
            if not isinstance(info, dict):
                continue

            occurrences = _expand_member_occurrences(info, start_bound, end_bound)
            status, occurrence = _current_or_next(occurrences, now)
            if not occurrence:
                continue

            course = occurrence.get("SUMMARY") or "未命名课程"
            location = occurrence.get("LOCATION")
            if location:
                course += f" @ {location}"
            time_text = f"{occurrence['_start']:%H:%M}-{occurrence['_end']:%H:%M}"
            name = names.get(user_id) or str(info.get("name") or user_id)
            rows.append((status, time_text, name, course))

        if not rows:
            return "当前会话没有可展示的当前或下一节课程。"

        rows.sort(key=lambda row: (row[0] != "正在上", row[1], row[2]))
        lines = ["群友当前 / 下一节课："]
        lines.extend(
            f"{name}：{status} {time_text} {course}"
            for status, time_text, name, course in rows
        )
        return "\n".join(lines)

    async def _group_current_image(self, event: AstrMessageEvent) -> str | None:
        members = await self._get_scope_members(event)
        if not members:
            return None

        now = datetime.now(LOCAL_TZ)
        start_bound, end_bound = _day_bounds(now.date())
        rows: list[dict[str, str]] = []
        names = await self._get_group_member_names(event, members)
        for user_id, info in members.items():
            if not isinstance(info, dict):
                continue
            occurrences = _expand_member_occurrences(info, start_bound, end_bound)
            status, occurrence = _current_or_next(occurrences, now)
            if not occurrence:
                continue

            course = occurrence.get("SUMMARY") or "未命名课程"
            location = occurrence.get("LOCATION")
            if location:
                course += f" @ {location}"
            rows.append(
                {
                    "user_id": user_id,
                    "name": names.get(user_id) or str(info.get("name") or user_id),
                    "subtitle": user_id,
                    "status": status,
                    "course": course,
                    "time": f"{occurrence['_start']:%H:%M}-{occurrence['_end']:%H:%M}",
                }
            )

        rows.sort(key=lambda row: (row["status"] != "正在上", row["time"], row["name"]))
        return _draw_rows_image("群友当前 / 下一节课", rows, "group_current.png")

    async def _group_tomorrow_image(self, event: AstrMessageEvent) -> str | None:
        members = await self._get_scope_members(event)
        if not members:
            return None

        target_date = datetime.now(LOCAL_TZ).date() + timedelta(days=1)
        start_bound, end_bound = _day_bounds(target_date)
        rows: list[dict[str, str]] = []
        names = await self._get_group_member_names(event, members)
        for user_id, info in members.items():
            if not isinstance(info, dict):
                continue
            occurrences = _expand_member_occurrences(info, start_bound, end_bound)
            if not occurrences:
                continue

            first = occurrences[0]
            course = first.get("SUMMARY") or "未命名课程"
            location = first.get("LOCATION")
            if location:
                course += f" @ {location}"
            rows.append(
                {
                    "user_id": user_id,
                    "name": names.get(user_id) or str(info.get("name") or user_id),
                    "subtitle": f"共 {len(occurrences)} 节",
                    "status": "明天",
                    "course": course,
                    "time": f"{first['_start']:%H:%M}-{first['_end']:%H:%M}",
                }
            )

        rows.sort(key=lambda row: (row["time"], row["name"]))
        return _draw_rows_image("群友明日课程", rows, "group_tomorrow.png")

    async def _weekly_rank_image(self, event: AstrMessageEvent) -> str | None:
        members = await self._get_scope_members(event)
        if not members:
            return None

        today = datetime.now(LOCAL_TZ).date()
        start_bound, end_bound = _week_bounds(today)
        rows: list[dict[str, str]] = []
        names = await self._get_group_member_names(event, members)
        for user_id, info in members.items():
            if not isinstance(info, dict):
                continue
            occurrences = _expand_member_occurrences(info, start_bound, end_bound)
            if not occurrences:
                continue

            hours = _duration_hours(occurrences)
            rows.append(
                {
                    "user_id": user_id,
                    "name": names.get(user_id) or str(info.get("name") or user_id),
                    "subtitle": user_id,
                    "status": f"{len(occurrences)} 节",
                    "course": f"{hours:.1f} 小时",
                    "time": f"{start_bound:%m/%d}-{(end_bound - timedelta(days=1)):%m/%d}",
                    "_hours": f"{hours:010.3f}",
                }
            )

        rows.sort(key=lambda row: (row["_hours"], row["status"]), reverse=True)
        for index, row in enumerate(rows, start=1):
            row["subtitle"] = f"第 {index} 名 · {row['subtitle']}"

        return _draw_rows_image("本周上课排行", rows, "weekly_rank.png")

    @filter.command("课程表", alias={"课表"})
    async def schedule(self, event: AstrMessageEvent):
        """课程表主命令"""
        rest = _strip_command(event.message_str, {"课程表", "课表"})
        parts = rest.split(maxsplit=1)
        action = parts[0].strip() if parts else ""
        arg = parts[1].strip() if len(parts) > 1 else ""

        if not action:
            yield event.plain_result(_help_text())
            return

        if action in {"帮助", "help"}:
            yield event.plain_result(_help_text())
            return

        if action in {"同步群文件", "同步", "sync"}:
            yield event.plain_result(await self._sync_group_files_text(event))
            return

        if action in {"导出", "上传", "export", "upload"}:
            yield event.plain_result(await self._export_own_ics_text(event))
            return

        if action in {"查看", "query", "show"}:
            yield event.plain_result(await self._show_schedule_text(event, arg))
            return

        if action in {"列表", "list"}:
            yield event.plain_result(await self._list_schedules_text(event))
            return

        if action in {"保存", "save"}:
            yield event.plain_result(await self._save_manual_schedule_text(event, arg))
            return

        if action in {"删除", "delete", "remove"}:
            yield event.plain_result(await self._delete_own_schedule_text(event))
            return

        yield event.plain_result("未知课程表命令，发送 /课程表 帮助 查看用法。")

    @filter.command("课程表帮助", alias={"课表帮助"})
    async def schedule_help(self, event: AstrMessageEvent):
        """查看课程表插件帮助"""
        yield event.plain_result(_help_text())

    @filter.command("查看课表")
    async def view_schedule(self, event: AstrMessageEvent):
        """快速查看自己完整课程表"""
        yield event.plain_result(await self._show_schedule_text(event))

    @filter.command("查看明日课表", alias={"查看明天课表"})
    async def view_tomorrow_schedule(self, event: AstrMessageEvent):
        """快速查看自己明天课程"""
        yield event.plain_result(await self._member_day_schedule_text(event, "", "tomorrow"))

    @filter.command("群友在上什么课")
    async def group_current_schedule(self, event: AstrMessageEvent):
        """显示群友当前正在上或下一节要上的课程"""
        path = await self._group_current_image(event)
        if not path:
            yield event.plain_result("当前会话还没有可展示的课程表。")
            return
        yield event.image_result(path)

    @filter.command("群友明天上什么课", alias={"群友明日上什么课"})
    async def group_tomorrow_schedule(self, event: AstrMessageEvent):
        """显示群友明天要上的课程"""
        path = await self._group_tomorrow_image(event)
        if not path:
            yield event.plain_result("当前会话还没有可展示的明日课程。")
            return
        yield event.image_result(path)

    @filter.command("本周上课排行")
    async def weekly_rank(self, event: AstrMessageEvent):
        """显示本周群友上课时长和节数排行"""
        path = await self._weekly_rank_image(event)
        if not path:
            yield event.plain_result("当前会话还没有可统计的课程表。")
            return
        yield event.image_result(path)

    @filter.command("课程表同步群文件", alias={"课表同步群文件"})
    async def sync_group_files(self, event: AstrMessageEvent):
        """同步群文件中的 schedule<QQ号>.ics"""
        yield event.plain_result(await self._sync_group_files_text(event))

    @filter.command("课程表导出", alias={"课表导出", "课程表上传", "课表上传"})
    async def export_schedule(self, event: AstrMessageEvent):
        """将自己的原始 .ics 上传到群文件"""
        yield event.plain_result(await self._export_own_ics_text(event))

    @filter.command("课程表查看", alias={"课表查看"})
    async def show_schedule(self, event: AstrMessageEvent):
        """查询自己或群友的完整课程表"""
        query = _strip_command(event.message_str, {"课程表查看", "课表查看"}).strip()
        yield event.plain_result(await self._show_schedule_text(event, query))

    @filter.command("课程表列表", alias={"课表列表"})
    async def list_schedules(self, event: AstrMessageEvent):
        """列出当前会话已保存课程表成员"""
        yield event.plain_result(await self._list_schedules_text(event))

    @filter.command("课程表保存", alias={"课表保存"})
    async def save_schedule(self, event: AstrMessageEvent):
        """手动保存文本课程表"""
        content = _strip_command(event.message_str, {"课程表保存", "课表保存"}).strip()
        yield event.plain_result(await self._save_manual_schedule_text(event, content))

    @filter.command("课程表删除", alias={"课表删除"})
    async def delete_schedule(self, event: AstrMessageEvent):
        """删除自己在当前会话中的课程表"""
        yield event.plain_result(await self._delete_own_schedule_text(event))

    @filter.llm_tool(name="query_course_schedule")
    async def query_course_schedule_tool(self, event: AstrMessageEvent, query: str = ""):
        """查询当前会话中已保存的完整课程表。按 QQ 号或昵称查询；query 为空时查询发起人的课程表。

        Args:
            query(string): 成员 QQ 号或昵称关键字。留空表示查询发起人自己的课程表。
        """
        yield event.plain_result(await self._show_schedule_text(event, query))

    @filter.llm_tool(name="query_course_schedule_day")
    async def query_course_schedule_day_tool(
        self, event: AstrMessageEvent, day: str = "today", query: str = ""
    ):
        """查询当前会话中某个成员某天的课程。仅 .ics 导入的课程表可按日期展开。

        Args:
            day(string): 日期，支持 today、tomorrow、今天、明天或 YYYY-MM-DD。
            query(string): 成员 QQ 号或昵称关键字。留空表示查询发起人自己的课程表。
        """
        yield event.plain_result(await self._member_day_schedule_text(event, query, day))

    @filter.llm_tool(name="list_course_schedules")
    async def list_course_schedules_tool(self, event: AstrMessageEvent):
        """列出当前会话中已经保存课程表的成员。"""
        yield event.plain_result(await self._list_schedules_text(event))

    @filter.llm_tool(name="query_group_current_courses")
    async def query_group_current_courses_tool(self, event: AstrMessageEvent):
        """查询当前会话中群友正在上或下一节要上的课程，返回文本摘要。"""
        yield event.plain_result(await self._group_current_text(event))
