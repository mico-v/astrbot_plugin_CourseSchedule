from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Any

from astrbot.api.event import AstrMessageEvent

from .constants import LOCAL_TZ, MAX_EVENTS_PER_FILE, MAX_ICS_BYTES
from .domain import (
    _display_name,
    daily_member_rows,
    make_event,
)
from .ics import (
    _format_ics_schedule,
    _parse_ics_datetime_obj,
    _parse_schedule_ics,
    _serialize_schedule_ics,
)
from .occurrences import _event_datetimes, _expand_event_occurrences
from .render import _draw_rows_image
from .sql_query import _parse_sql_time_range
from .sqlite_store import ScheduleWriteConflict, SQLiteScheduleStore
from .store import _scope_id
from .texts import _is_own_query
from .time_utils import _now_iso


class CourseScheduleBase:
    """Course-schedule application service shared by AstrBot entry points."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._schedule_store = SQLiteScheduleStore()

    async def _get_scope_members(self, event: AstrMessageEvent) -> dict[str, Any]:
        return await self._schedule_store.get_scope_members(_scope_id(event))

    @staticmethod
    def _web_datetime_value(value: str, tzid: str = "") -> str:
        parsed = _parse_ics_datetime_obj(value, tzid)
        return parsed.strftime("%Y-%m-%dT%H:%M") if parsed else ""

    @classmethod
    def _web_event_payload(cls, index: int, event: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": index,
            "uid": str(event.get("UID") or ""),
            "course": str(event.get("SUMMARY") or ""),
            "location": str(event.get("LOCATION") or ""),
            "description": str(event.get("DESCRIPTION") or ""),
            "start": cls._web_datetime_value(
                str(event.get("DTSTART") or ""), str(event.get("DTSTART_TZID") or "")
            ),
            "end": cls._web_datetime_value(
                str(event.get("DTEND") or ""), str(event.get("DTEND_TZID") or "")
            ),
            "rrule": str(event.get("RRULE") or ""),
        }

    async def _page_scopes(self) -> dict[str, Any]:
        scopes = await self._schedule_store.list_scope_summaries()
        result: list[dict[str, Any]] = []
        for scope in scopes:
            scope_id = str(scope.get("scope_id") or "")
            if ":" in scope_id:
                kind, target_id = scope_id.split(":", 1)
            else:
                kind, target_id = "other", scope_id
            members = []
            for member in scope.get("members", []):
                if not isinstance(member, dict):
                    continue
                members.append(
                    {
                        "user_id": str(member.get("user_id") or ""),
                        "name": _display_name(
                            member.get("name") or member.get("user_id")
                        ),
                        "event_count": int(member.get("event_count") or 0),
                        "revision": int(member.get("revision") or 0),
                    }
                )
            result.append(
                {
                    "scope_id": scope_id,
                    "kind": kind,
                    "target_id": target_id,
                    "label": (
                        f"{'群聊' if kind == 'group' else '私聊' if kind == 'private' else kind} "
                        f"{target_id}"
                    ),
                    "member_count": len(members),
                    "event_count": sum(item["event_count"] for item in members),
                    "members": members,
                }
            )
        result.sort(
            key=lambda item: (0 if item["kind"] == "group" else 1, item["target_id"])
        )
        return {"scopes": result}

    async def _page_schedule(self, scope_id: str, user_id: str) -> dict[str, Any] | None:
        scope = str(scope_id or "").strip()
        member_id = str(user_id or "").strip()
        if not scope or not member_id:
            raise ValueError("scope_id 和 user_id 不能为空。")
        info = await self._schedule_store.get_member(scope, member_id)
        if not isinstance(info, dict):
            return None
        events = [item for item in info.get("events", []) if isinstance(item, dict)]
        return {
            "scope_id": scope,
            "user_id": member_id,
            "name": _display_name(info.get("name") or member_id),
            "revision": int(info.get("_revision") or 0),
            "events": [
                self._web_event_payload(index, event)
                for index, event in enumerate(events, 1)
            ],
        }

    async def _save_page_schedule(
        self, payload: dict[str, Any], *, actor: str = "webui"
    ) -> dict[str, Any]:
        scope_id = str(payload.get("scope_id") or "").strip()
        user_id = str(payload.get("user_id") or "").strip()
        if not scope_id or not user_id:
            raise ValueError("scope_id 和 user_id 不能为空。")
        if not (scope_id.startswith("group:") or scope_id.startswith("private:")):
            raise ValueError("无效的 scope_id。")
        try:
            expected_revision = int(payload.get("revision"))
        except (TypeError, ValueError) as exc:
            raise ValueError("缺少有效的课程表 revision，请刷新后重试。") from exc

        raw_events = payload.get("events")
        if not isinstance(raw_events, list):
            raise ValueError("events 必须是数组。")
        if len(raw_events) > MAX_EVENTS_PER_FILE:
            raise ValueError(f"单个成员最多保存 {MAX_EVENTS_PER_FILE} 节课程。")

        current = await self._schedule_store.get_member(scope_id, user_id)
        if not isinstance(current, dict):
            raise ValueError("找不到指定群组中的成员课程表。")
        current_events = [
            item for item in current.get("events", []) if isinstance(item, dict)
        ]
        events: list[dict[str, str]] = []
        for index, raw in enumerate(raw_events, 1):
            if not isinstance(raw, dict):
                raise ValueError(f"第 {index} 节课程格式无效。")
            course = str(raw.get("course") or "").strip()
            start = str(raw.get("start") or "").strip()
            end = str(raw.get("end") or "").strip()
            if not course:
                raise ValueError(f"第 {index} 节课程缺少课程名称。")
            if len(course) > 200:
                raise ValueError(f"第 {index} 节课程名称不能超过 200 个字符。")
            location = str(raw.get("location") or "").strip()
            description = str(raw.get("description") or "").strip()
            rrule = str(raw.get("rrule") or "").strip()
            if len(location) > 200 or len(description) > 2000 or len(rrule) > 500:
                raise ValueError(f"第 {index} 节课程的文本字段过长。")
            try:
                event = make_event(
                    course,
                    start,
                    end,
                    location=location,
                    description=description,
                    rrule=rrule,
                    uid=str(raw.get("uid") or "").strip(),
                )
            except ValueError as exc:
                raise ValueError(f"第 {index} 节课程：{exc}") from exc
            original = None
            uid = str(raw.get("uid") or "").strip()
            if uid:
                original = next(
                    (item for item in current_events if str(item.get("UID") or "") == uid),
                    None,
                )
            if original is None:
                try:
                    original_index = int(raw.get("id")) - 1
                except (TypeError, ValueError):
                    original_index = -1
                if 0 <= original_index < len(current_events):
                    original = current_events[original_index]
            if original and original.get("RAW_ICAL"):
                event["RAW_ICAL"] = str(original["RAW_ICAL"])
            events.append(event)
        events.sort(key=lambda item: (item.get("DTSTART", ""), item.get("DTEND", "")))

        now = _now_iso()
        updated = dict(current)
        raw_name = payload.get("name")
        if raw_name is not None:
            name = _display_name(raw_name)
            if not name:
                raise ValueError("成员名称不能为空。")
            if len(name) > 200:
                raise ValueError("成员名称不能超过 200 个字符。")
            updated["name"] = name
        updated["events"] = events
        updated["event_count"] = len(events)
        updated["ics"] = _serialize_schedule_ics(events, str(current.get("ics") or ""))
        updated["schedule"] = _format_ics_schedule(events)
        updated["source"] = "ics"
        updated["updated_at"] = now
        updated["schedule_updated_at"] = now
        updated["last_modified_at"] = now
        updated["last_modified_by"] = str(actor or "webui")
        await self._schedule_store.put_member(
            scope_id,
            user_id,
            updated,
            expected_revision=expected_revision,
        )
        saved = await self._schedule_store.get_member(scope_id, user_id)
        return {
            "scope_id": scope_id,
            "user_id": user_id,
            "name": _display_name(updated.get("name") or user_id),
            "revision": (
                int(saved.get("_revision") or expected_revision + 1)
                if saved
                else expected_revision + 1
            ),
            "event_count": len(events),
        }

    async def _save_ics_schedule(
        self,
        event: AstrMessageEvent,
        ics_content: str,
        *,
        user_id: str | None = None,
        name: str | None = None,
        source_file: str = "",
        uploader_id: str | None = None,
    ) -> str:
        """Parse and persist one ICS document.

        This is the import boundary reserved for the future "file reference +
        @bot" message handler. It does not fetch, upload, or delete group files.
        """
        content = str(ics_content or "").strip()
        if not content:
            return "ICS 内容为空，未保存。"
        if len(content.encode("utf-8")) > MAX_ICS_BYTES:
            return f"ICS 文件超过 {MAX_ICS_BYTES // 1024 // 1024} MiB，未保存。"

        try:
            events, schedule_text = _parse_schedule_ics(content)
        except ValueError as exc:
            return f"ICS 解析失败，未保存：{exc}"
        if not events:
            return "ICS 中没有 VEVENT，未保存。"

        target_id = str(user_id or event.get_sender_id())
        scope = _scope_id(event)
        previous = await self._schedule_store.get_member(scope, target_id) or {}
        now = _now_iso()
        info = {
            "name": name or previous.get("name") or event.get_sender_name() or target_id,
            "schedule": schedule_text,
            "updated_at": now,
            "schedule_updated_at": now,
            "source": "ics",
            "source_file": source_file,
            "uploader_id": uploader_id or event.get_sender_id(),
            "event_count": len(events),
            "events": events,
            "ics": content,
        }
        try:
            await self._schedule_store.put_member(
                scope,
                target_id,
                info,
                expected_revision=int(previous.get("_revision") or 0),
            )
        except ScheduleWriteConflict as exc:
            return str(exc)
        return f"已保存 {info['name']}({target_id}) 的课程表，共 {len(events)} 个事件。"

    @staticmethod
    def _agent_action(value: str) -> str:
        normalized = str(value or "").strip().lower()
        return {
            "create": "create",
            "add": "create",
            "new": "create",
            "create_course": "create",
            "新增": "create",
            "添加": "create",
            "增加": "create",
            "新增课程": "create",
            "建立": "create",
            "update": "update",
            "edit": "update",
            "modify": "update",
            "update_course": "update",
            "修改": "update",
            "编辑": "update",
            "更改": "update",
            "修改课程": "update",
            "delete": "delete",
            "remove": "delete",
            "del": "delete",
            "delete_course": "delete",
            "删除": "delete",
            "移除": "delete",
            "删除课程": "delete",
        }.get(normalized, "")

    @staticmethod
    def _agent_field(value: str) -> str:
        normalized = str(value or "").strip().lower().replace(" ", "")
        return {
            "course": "course",
            "课程": "course",
            "课程名": "course",
            "课程名称": "course",
            "summary": "course",
            "location": "location",
            "地点": "location",
            "教室": "location",
            "description": "description",
            "备注": "description",
            "说明": "description",
            "note": "description",
            "status": "status",
            "状态": "status",
            "date": "date",
            "日期": "date",
            "day": "date",
            "weekday": "weekday",
            "星期": "weekday",
            "周几": "weekday",
            "user_id": "user_id",
            "userid": "user_id",
            "qq": "user_id",
            "qq号": "user_id",
            "成员": "member",
            "人": "member",
            "member": "member",
            "name": "member",
            "昵称": "member",
            "start": "start_time",
            "开始": "start_time",
            "开始时间": "start_time",
            "start_time": "start_time",
            "end": "end_time",
            "结束": "end_time",
            "结束时间": "end_time",
            "end_time": "end_time",
            "duration": "duration",
            "时长": "duration",
            "duration_minutes": "duration",
            "rrule": "rrule",
            "重复": "rrule",
            "重复规则": "rrule",
            "来源文件": "source_file",
            "source_file": "source_file",
        }.get(normalized, "")

    @staticmethod
    def _agent_text(value: Any) -> str:
        return _display_name(value).strip()

    @classmethod
    def _agent_time_range(
        cls, value: str, now: datetime
    ) -> tuple[datetime, datetime, str] | None:
        """Parse find's optional range; None means all stored event definitions."""
        raw = str(value or "").strip()
        if not raw or raw.lower() in {"all", "全部", "所有", "任意", "*"}:
            return None

        # The established date-range parser handles relative dates and Chinese
        # ranges.  Keep it as the common path so find and the legacy SQL helper
        # interpret today/本周/日期范围 identically.
        try:
            return _parse_sql_time_range(raw, now.date())
        except ValueError as date_error:
            pass

        range_parts = [
            part.strip()
            for part in re.split(r"\s*(?:\.\.|~|至|到|—|\bto\b)\s*", raw, maxsplit=1, flags=re.IGNORECASE)
            if part.strip()
        ]
        try:
            if len(range_parts) == 1:
                start = datetime.fromisoformat(range_parts[0].replace("Z", "+00:00"))
                end = start + timedelta(minutes=1)
            elif len(range_parts) == 2:
                start = datetime.fromisoformat(range_parts[0].replace("Z", "+00:00"))
                end = datetime.fromisoformat(range_parts[1].replace("Z", "+00:00"))
            else:
                raise ValueError
            if start.tzinfo is None:
                start = start.replace(tzinfo=LOCAL_TZ)
            if end.tzinfo is None:
                end = end.replace(tzinfo=LOCAL_TZ)
            start = start.astimezone(LOCAL_TZ)
            end = end.astimezone(LOCAL_TZ)
            if end <= start:
                raise ValueError("时间范围的结束时间必须晚于开始时间。")
            label = f"{start:%Y-%m-%d %H:%M}..{end:%Y-%m-%d %H:%M}"
            return start, end, label
        except ValueError as exc:
            raise ValueError(
                "无法解析 time_range，请使用 today、YYYY-MM-DD 或 "
                "YYYY-MM-DD..YYYY-MM-DD，也可以使用完整日期时间范围。"
            ) from (date_error if "date_error" in locals() else exc)

    @staticmethod
    def _agent_status(start: datetime, end: datetime, now: datetime) -> str:
        if start <= now < end:
            return "current"
        return "future" if start > now else "past"

    @staticmethod
    def _agent_weekday(value: str) -> int | None:
        normalized = str(value or "").strip().lower()
        aliases = {
            "1": 1,
            "一": 1,
            "周一": 1,
            "星期一": 1,
            "mon": 1,
            "monday": 1,
            "2": 2,
            "二": 2,
            "周二": 2,
            "星期二": 2,
            "tue": 2,
            "tuesday": 2,
            "3": 3,
            "三": 3,
            "周三": 3,
            "星期三": 3,
            "wed": 3,
            "wednesday": 3,
            "4": 4,
            "四": 4,
            "周四": 4,
            "星期四": 4,
            "thu": 4,
            "thursday": 4,
            "5": 5,
            "五": 5,
            "周五": 5,
            "星期五": 5,
            "fri": 5,
            "friday": 5,
            "6": 6,
            "六": 6,
            "周六": 6,
            "星期六": 6,
            "sat": 6,
            "saturday": 6,
            "7": 7,
            "日": 7,
            "天": 7,
            "周日": 7,
            "星期日": 7,
            "周天": 7,
            "星期天": 7,
            "sun": 7,
            "sunday": 7,
        }
        return aliases.get(normalized)

    @classmethod
    def _agent_member_ids(
        cls,
        members: dict[str, Any],
        person: str,
        sender_id: str,
        *,
        allow_unknown_create: bool = False,
        allow_all: bool = False,
    ) -> tuple[list[str], str | None]:
        query = cls._agent_text(person)
        if not query:
            return [str(sender_id)], None
        if _is_own_query(query):
            return ([str(sender_id)], None)
        if allow_all and query.lower() in {"all", "全部", "所有", "大家"}:
            return [str(user_id) for user_id in members], None

        exact_ids = [user_id for user_id in members if str(user_id) == query]
        if exact_ids:
            return exact_ids, None

        exact_names = [
            str(user_id)
            for user_id, info in members.items()
            if isinstance(info, dict)
            and cls._agent_text(info.get("name") or user_id).casefold()
            == query.casefold()
        ]
        if len(exact_names) == 1:
            return exact_names, None
        if len(exact_names) > 1:
            labels = [
                f"{cls._agent_text(members[user_id].get('name') or user_id)}({user_id})"
                for user_id in exact_names[:10]
            ]
            return [], "昵称存在多个精确匹配，请改用 QQ 号：\n" + "\n".join(labels)
        if allow_unknown_create and query.isdigit():
            return [query], None
        return [], f"没有找到成员“{query}”的课程表，请使用 QQ 号或完整昵称。"

    @classmethod
    def _agent_clear_fields(cls, value: str) -> tuple[set[str], str | None]:
        fields: set[str] = set()
        for item in re.split(r"[,，、\s]+", str(value or "")):
            if not item:
                continue
            field = cls._agent_field(item)
            if field not in {"location", "description", "rrule"}:
                return set(), f"clear_fields 不支持清空“{item}”，只能清空地点、备注或重复规则。"
            fields.add(field)
        return fields, None

    async def _find_schedule_text(
        self,
        event: AstrMessageEvent,
        person: str = "",
        time_range: str = "",
        field: str = "",
        value: str = "",
    ) -> str:
        members = await self._get_scope_members(event)
        if not members:
            return "当前会话还没有保存任何课程表。"

        # Accept the natural compact form field="course:数学" as well as the
        # explicit field/value arguments exposed in the tool schema.
        raw_field = str(field or "").strip()
        raw_value = str(value or "").strip()
        if not raw_value and (":" in raw_field or "=" in raw_field):
            separator = ":" if ":" in raw_field else "="
            raw_field, raw_value = raw_field.split(separator, 1)
        field_name = self._agent_field(raw_field)
        if raw_field and raw_field.lower() not in {"all", "全部", "所有"} and not field_name:
            return "不支持的查找字段，请使用 course、location、description、status、date、weekday、member 或 user_id。"
        if field_name and not raw_value:
            return "使用 field 查找时必须填写 value。"
        if raw_value and not field_name:
            return "使用 value 查找时必须同时填写 field，例如 field=course、value=数学。"

        sender_id = str(event.get_sender_id())
        selected_ids, member_error = self._agent_member_ids(
            members, person, sender_id, allow_all=True
        )
        if member_error:
            return member_error
        selected_ids = [user_id for user_id in selected_ids if user_id in members]
        if person and not selected_ids:
            return "你还没有保存课程表。" if _is_own_query(person) else f"没有找到成员“{person}”的课程表。"

        now = datetime.now(LOCAL_TZ)
        try:
            parsed_range = self._agent_time_range(time_range, now)
            if not parsed_range and field_name == "date":
                try:
                    date.fromisoformat(raw_value)
                except ValueError as exc:
                    return "date value 应使用 YYYY-MM-DD 格式；日期范围请使用 time_range。"
                parsed_range = self._agent_time_range(raw_value, now)
        except ValueError as exc:
            return str(exc)

        start_bound = end_bound = None
        range_label = "全部已保存事件"
        if parsed_range:
            start_bound, end_bound, range_label = parsed_range

        normalized_value = self._agent_text(raw_value).casefold()
        weekday_value = self._agent_weekday(raw_value) if field_name == "weekday" else None
        if field_name == "weekday" and weekday_value is None:
            return "weekday value 应为周一到周日，例如 周一、1 或 Monday。"

        member_summaries: list[str] = []
        rows: list[dict[str, Any]] = []
        weekday_names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
        for user_id in selected_ids:
            info = members.get(user_id)
            if not isinstance(info, dict):
                continue
            member_name = self._agent_text(info.get("name") or user_id)
            source_events = [
                item for item in info.get("events", []) if isinstance(item, dict)
            ]
            member_summaries.append(
                f"{member_name}({user_id})：{len(source_events)} 个课程事件"
            )

            occurrences: list[tuple[int, dict[str, Any]]] = []
            if start_bound is None or end_bound is None:
                for index, source_event in enumerate(source_events, 1):
                    start, end = _event_datetimes(source_event)
                    if start and end:
                        occurrences.append((index, {**source_event, "_start": start, "_end": end}))
            else:
                for index, source_event in enumerate(source_events, 1):
                    for occurrence in _expand_event_occurrences(
                        source_event, start_bound, end_bound
                    ):
                        occurrences.append((index, occurrence))

            for course_id, occurrence in occurrences:
                start = occurrence["_start"]
                end = occurrence["_end"]
                course = self._agent_text(occurrence.get("SUMMARY") or "未命名课程")
                location = self._agent_text(occurrence.get("LOCATION"))
                description = self._agent_text(occurrence.get("DESCRIPTION"))
                status = self._agent_status(start, end, now)
                duration_minutes = max(1, round((end - start).total_seconds() / 60))
                row = {
                    "course_id": course_id,
                    "user_id": user_id,
                    "member": member_name,
                    "course": course,
                    "location": location,
                    "description": description,
                    "start": start,
                    "end": end,
                    "date": f"{start:%Y-%m-%d}",
                    "weekday": start.isoweekday(),
                    "weekday_name": weekday_names[start.weekday()],
                    "duration": duration_minutes,
                    "status": status,
                    "rrule": self._agent_text(occurrence.get("RRULE")),
                }
                if field_name:
                    if field_name == "course" and normalized_value not in course.casefold():
                        continue
                    if field_name == "location" and normalized_value not in location.casefold():
                        continue
                    if field_name == "description" and normalized_value not in description.casefold():
                        continue
                    if field_name == "rrule" and normalized_value not in row["rrule"].casefold():
                        continue
                    if field_name == "member" and member_name.casefold() != normalized_value:
                        continue
                    if field_name == "source_file":
                        source_file = self._agent_text(info.get("source_file"))
                        if normalized_value not in source_file.casefold():
                            continue
                    if field_name == "user_id" and str(user_id) != raw_value:
                        continue
                    if field_name == "status" and status != raw_value.lower():
                        status_alias = {
                            "正在上课": "current",
                            "当前": "current",
                            "进行中": "current",
                            "ongoing": "current",
                            "未来": "future",
                            "将要上课": "future",
                            "未开始": "future",
                            "已结束": "past",
                            "过去": "past",
                            "finished": "past",
                        }
                        if status_alias.get(raw_value.lower()) != status:
                            continue
                    if field_name == "date" and row["date"] != raw_value:
                        continue
                    if field_name == "weekday" and row["weekday"] != weekday_value:
                        continue
                    if field_name == "duration":
                        try:
                            if row["duration"] != int(raw_value):
                                continue
                        except ValueError:
                            return "duration value 应为分钟数，例如 90。"
                    if field_name == "start_time" and f"{start:%Y-%m-%d %H:%M}" != raw_value:
                        continue
                    if field_name == "end_time" and f"{end:%Y-%m-%d %H:%M}" != raw_value:
                        continue
                rows.append(row)

        rows.sort(key=lambda row: (row["start"], row["member"].casefold(), row["user_id"], row["course_id"]))
        lines = [f"查询范围：{range_label}"]
        all_requested = self._agent_text(person).lower() in {
            "all", "全部", "所有", "大家"
        }
        if all_requested or (not person and not field_name and not parsed_range):
            lines.append(f"成员列表（{len(member_summaries)} 人）：")
            lines.extend(f"- {summary}" for summary in member_summaries)
        if not rows:
            lines.append("没有找到符合条件的课程。")
            return "\n".join(lines)

        max_rows = 200
        shown = rows[:max_rows]
        lines.append(f"课程结果（{len(rows)} 条" + (f"，展示前 {max_rows} 条" if len(rows) > max_rows else "") + "）：")
        for row in shown:
            details = [
                f"course_id={row['course_id']}",
                f"{row['start']:%Y-%m-%d %H:%M}-{row['end']:%H:%M}",
                row["course"],
                f"状态：{row['status']}",
                f"时长：{row['duration']}分钟",
            ]
            if row["location"]:
                details.append(f"地点：{row['location']}")
            if row["description"]:
                details.append(f"备注：{row['description']}")
            if row["rrule"]:
                details.append(f"重复：{row['rrule']}")
            lines.append(f"- {row['member']}({row['user_id']}) | " + " | ".join(details))
        if len(rows) > max_rows:
            lines.append(f"结果超过 {max_rows} 条，请缩小 person、time_range 或 field/value。")
        return "\n".join(lines)

    async def _edit_schedule_text(
        self,
        event: AstrMessageEvent,
        action: str,
        *,
        person: str = "",
        course_id: int = 0,
        course: str = "",
        start_time: str = "",
        end_time: str = "",
        location: str = "",
        description: str = "",
        rrule: str = "",
        member_name: str = "",
        clear_fields: str = "",
    ) -> str:
        operation = self._agent_action(action)
        if not operation:
            return "action 只能是 create/add、update/edit 或 delete/remove（也可使用新增、修改、删除）。"

        scope = _scope_id(event)
        members = await self._get_scope_members(event)
        sender_id = str(event.get_sender_id())
        target_ids, member_error = self._agent_member_ids(
            members,
            person,
            sender_id,
            allow_unknown_create=operation == "create",
        )
        if member_error:
            return member_error
        if not target_ids:
            return "没有确定要修改的成员。"
        target_id = target_ids[0]
        group_id = str(event.get_group_id() or "").strip()
        is_admin = False
        checker = getattr(event, "is_admin", None)
        if callable(checker):
            try:
                is_admin = bool(checker())
            except Exception:
                is_admin = False
        if target_id != sender_id and (not group_id or not is_admin):
            if group_id:
                return "普通成员只能修改自己的课表；修改群内其他成员的课表需要管理员权限。"
            return "私聊只能修改自己的课表。"

        info = members.get(target_id)
        if operation != "create" and not isinstance(info, dict):
            return f"没有找到成员“{person or target_id}”的课程表。"
        if not isinstance(info, dict):
            info = {
                "name": self._agent_text(member_name)
                or self._agent_text(event.get_sender_name())
                or target_id,
                "events": [],
                "ics": "",
                "source": "manual",
            }

        new_name = self._agent_text(member_name)
        if new_name:
            if len(new_name) > 200:
                return "member_name 不能超过 200 个字符。"
            info = dict(info)
            info["name"] = new_name

        try:
            clear, clear_error = self._agent_clear_fields(clear_fields)
            if clear_error:
                return clear_error
            events = [dict(item) for item in info.get("events", []) if isinstance(item, dict)]
            if operation == "create":
                course_text = self._agent_text(course)
                if not course_text or not str(start_time or "").strip() or not str(end_time or "").strip():
                    return "新增课程必须填写 course、start_time 和 end_time。"
                if len(events) >= MAX_EVENTS_PER_FILE:
                    return f"单个成员最多保存 {MAX_EVENTS_PER_FILE} 个课程事件。"
                if len(course_text) > 200:
                    return "course 不能超过 200 个字符。"
                if len(str(location or "")) > 200 or len(str(description or "")) > 2000 or len(str(rrule or "")) > 500:
                    return "location、description 或 rrule 的内容过长。"
                events.append(
                    make_event(
                        course_text,
                        start_time,
                        end_time,
                        location=self._agent_text(location),
                        description=self._agent_text(description),
                        rrule=str(rrule or "").strip(),
                    )
                )
                events.sort(key=lambda item: (item.get("DTSTART", ""), item.get("DTEND", "")))
                await self._save_domain_events(event, target_id, info, events)
                return f"已新增 {info.get('name') or target_id}({target_id}) 的课程“{course_text}”，当前共有 {len(events)} 个课程事件。"

            try:
                index = int(course_id) - 1
            except (TypeError, ValueError):
                index = -1
            if operation == "delete":
                if index < 0 or index >= len(events):
                    return f"course_id 必须是有效的课程编号，当前有效范围为 1..{len(events)}。"
                removed = events.pop(index)
                await self._save_domain_events(event, target_id, info, events)
                return f"已删除 {info.get('name') or target_id}({target_id}) 的课程“{removed.get('SUMMARY') or course_id}”，剩余 {len(events)} 个课程事件。"

            if index < 0:
                if not new_name:
                    return "修改课程必须填写来自 find 结果的 course_id；仅修改昵称时才可以省略 course_id。"
                await self._save_domain_events(event, target_id, info, events)
                return f"已更新成员名称为 {info.get('name') or target_id}({target_id})。"

            if index >= len(events):
                return f"course_id 超出范围，当前有效范围为 1..{len(events)}。"
            old = events[index]
            if len(str(course or "")) > 200 or len(str(location or "")) > 200 or len(str(description or "")) > 2000 or len(str(rrule or "")) > 500:
                return "course、location、description 或 rrule 的内容过长。"
            updated = make_event(
                self._agent_text(course) or str(old.get("SUMMARY") or ""),
                str(start_time or "").strip() or str(old.get("DTSTART") or ""),
                str(end_time or "").strip() or str(old.get("DTEND") or ""),
                location="" if "location" in clear else (self._agent_text(location) or str(old.get("LOCATION") or "")),
                description="" if "description" in clear else (self._agent_text(description) or str(old.get("DESCRIPTION") or "")),
                rrule="" if "rrule" in clear else (str(rrule or "").strip() or str(old.get("RRULE") or "")),
                uid=str(old.get("UID") or ""),
            )
            if old.get("RAW_ICAL"):
                updated["RAW_ICAL"] = str(old["RAW_ICAL"])
            events[index] = updated
            events.sort(key=lambda item: (item.get("DTSTART", ""), item.get("DTEND", "")))
            await self._save_domain_events(event, target_id, info, events)
            return f"已更新 {info.get('name') or target_id}({target_id}) 的课程 course_id={course_id}，当前共有 {len(events)} 个课程事件。"
        except (ValueError, ScheduleWriteConflict) as exc:
            return str(exc)

    async def _save_domain_events(
        self,
        event: AstrMessageEvent,
        target_id: str,
        info: dict[str, Any],
        events: list[dict[str, str]],
    ) -> None:
        now = _now_iso()
        updated = dict(info)
        updated["name"] = updated.get("name") or event.get_sender_name() or target_id
        updated["events"] = events
        updated["event_count"] = len(events)
        updated["ics"] = _serialize_schedule_ics(events, str(info.get("ics") or ""))
        updated["schedule"] = _format_ics_schedule(events)
        updated["source"] = "ics"
        updated["updated_at"] = now
        updated["schedule_updated_at"] = now
        updated["last_modified_by"] = event.get_sender_id()
        await self._schedule_store.put_member(
            _scope_id(event),
            target_id,
            updated,
            expected_revision=int(info.get("_revision") or 0),
        )

    async def _group_schedule_image(
        self, event: AstrMessageEvent, target_date: date | None = None
    ) -> str | None:
        members = await self._get_scope_members(event)
        if not members:
            return None

        now = datetime.now(LOCAL_TZ)
        selected_date = target_date or now.date()
        rows = daily_member_rows(members, selected_date, now=now)
        return _draw_rows_image(
            f"课程表 · {selected_date:%Y-%m-%d}",
            rows,
            f"schedule_{selected_date:%Y%m%d}.png",
        )

    async def _group_today_image(self, event: AstrMessageEvent) -> str | None:
        return await self._group_schedule_image(event)
