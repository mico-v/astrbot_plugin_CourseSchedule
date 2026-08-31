from __future__ import annotations

import asyncio
import re
from datetime import date, datetime, time
from typing import Any

from astrbot.api.event import AstrMessageEvent

from .constants import LOCAL_TZ, MAX_ICS_BYTES
from .domain import common_free_slots, day_occurrences, make_event, select_member_ids
from .ics import _format_ics_schedule, _parse_schedule_ics, _serialize_schedule_ics
from .occurrences import _day_bounds, _expand_member_occurrences
from .render import _draw_rows_image
from .sql_edit import apply_sql_edit_to_member
from .sql_query import execute_course_schedule_sql
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
                labels = [
                    f"{members[user_id].get('name') or user_id}({user_id})"
                    for user_id in matched_ids[:10]
                ]
                return None, None, "找到多个匹配成员，请用 QQ 号精确查询：\n" + "\n".join(labels)
            target_id = matched_ids[0]

        info = members.get(target_id)
        if not isinstance(info, dict):
            return None, None, "你还没有保存课程表。"
        return target_id, info, None

    async def _edit_local_schedule_sql_text(
        self, event: AstrMessageEvent, sql: str, query: str = ""
    ) -> str:
        target_id, _resolved, error = await self._resolve_member_info(event, query)
        if error:
            return error

        scope = _scope_id(event)
        info = await self._schedule_store.get_member(scope, str(target_id))
        if not isinstance(info, dict) or not info.get("ics"):
            return "当前课程表不是结构化 ICS 数据，不能用 SQL 修改。"

        try:
            updated_info = apply_sql_edit_to_member(info, sql)
        except ValueError as exc:
            return str(exc)

        changes = updated_info.pop("_sql_edit_changes", 0)
        updated_info["schedule"] = _format_ics_schedule(updated_info["events"])
        updated_info["last_modified_by"] = event.get_sender_id()
        try:
            await self._schedule_store.put_member(
                scope,
                str(target_id),
                updated_info,
                expected_revision=int(info.get("_revision") or 0),
            )
        except ScheduleWriteConflict as exc:
            return str(exc)

        name = updated_info.get("name") or target_id
        return (
            f"已用 SQL 修改 {name}({target_id}) 的本地课程表，影响 {changes} 条，"
            f"当前共有 {updated_info.get('event_count', 0)} 个事件。"
        )

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

    async def _create_course_text(
        self,
        event: AstrMessageEvent,
        course: str,
        start_time: str,
        end_time: str,
        location: str = "",
        description: str = "",
        rrule: str = "",
    ) -> str:
        target_id = str(event.get_sender_id())
        info = await self._schedule_store.get_member(_scope_id(event), target_id) or {}
        events = [dict(item) for item in info.get("events", []) if isinstance(item, dict)]
        try:
            events.append(
                make_event(
                    course, start_time, end_time,
                    location=location, description=description, rrule=rrule,
                )
            )
            events.sort(key=lambda item: item.get("DTSTART", ""))
            await self._save_domain_events(event, target_id, info, events)
        except (ValueError, ScheduleWriteConflict) as exc:
            return str(exc)
        return f"已创建课程“{course}”，当前共有 {len(events)} 个课程事件。"

    async def _update_course_text(
        self, event: AstrMessageEvent, course_id: int, query: str = "", **changes: str
    ) -> str:
        target_id, info, error = await self._resolve_member_info(event, query)
        if error or not target_id or not info:
            return error or "未找到课程表。"
        events = [dict(item) for item in info.get("events", []) if isinstance(item, dict)]
        index = int(course_id) - 1
        if index < 0 or index >= len(events):
            return f"course_id 超出范围，当前有效范围为 1..{len(events)}。"
        old = events[index]
        try:
            events[index] = make_event(
                changes.get("course") or old.get("SUMMARY", ""),
                changes.get("start_time") or old.get("DTSTART", ""),
                changes.get("end_time") or old.get("DTEND", ""),
                location=changes.get("location") if changes.get("location") != "" else old.get("LOCATION", ""),
                description=changes.get("description") if changes.get("description") != "" else old.get("DESCRIPTION", ""),
                rrule=changes.get("rrule") if changes.get("rrule") != "" else old.get("RRULE", ""),
                uid=old.get("UID", ""),
            )
            events[index]["RAW_ICAL"] = old.get("RAW_ICAL", "")
            events.sort(key=lambda item: item.get("DTSTART", ""))
            await self._save_domain_events(event, target_id, info, events)
        except (ValueError, ScheduleWriteConflict) as exc:
            return str(exc)
        return f"已更新 {info.get('name') or target_id} 的 course_id={course_id}。"

    async def _delete_course_text(
        self, event: AstrMessageEvent, course_id: int, query: str = ""
    ) -> str:
        target_id, info, error = await self._resolve_member_info(event, query)
        if error or not target_id or not info:
            return error or "未找到课程表。"
        events = [dict(item) for item in info.get("events", []) if isinstance(item, dict)]
        index = int(course_id) - 1
        if index < 0 or index >= len(events):
            return f"course_id 超出范围，当前有效范围为 1..{len(events)}。"
        removed = events.pop(index)
        try:
            await self._save_domain_events(event, target_id, info, events)
        except ScheduleWriteConflict as exc:
            return str(exc)
        return f"已删除课程“{removed.get('SUMMARY') or course_id}”，剩余 {len(events)} 个事件。"

    async def _daily_schedule_text(
        self, event: AstrMessageEvent, target_date: str = "", members_query: str = ""
    ) -> str:
        members = await self._get_scope_members(event)
        if not members:
            return "当前会话还没有保存任何课程表。"
        try:
            target = date.fromisoformat(target_date) if target_date else datetime.now(LOCAL_TZ).date()
        except ValueError:
            return "target_date 应使用 YYYY-MM-DD 格式。"
        ids = select_member_ids(members, members_query)
        if members_query and not ids:
            return "没有找到指定成员。"
        rows = day_occurrences(members, target, ids)
        if not rows:
            return f"{target:%Y-%m-%d} 没有课程。"
        lines = [f"{target:%Y-%m-%d} 课程（{len(rows)} 条）："]
        for user_id, info, occurrence in rows:
            lines.append(
                f"{occurrence['_start']:%H:%M}-{occurrence['_end']:%H:%M} "
                f"{info.get('name') or user_id}：{occurrence.get('SUMMARY') or '未命名课程'}"
                + (f" @{occurrence['LOCATION']}" if occurrence.get("LOCATION") else "")
            )
        return "\n".join(lines)

    async def _common_free_time_text(
        self, event: AstrMessageEvent, target_date: str, members_query: str = "",
        day_start: str = "08:00", day_end: str = "22:00", minimum_minutes: int = 30,
    ) -> str:
        members = await self._get_scope_members(event)
        try:
            target = date.fromisoformat(target_date)
            start_clock = time.fromisoformat(day_start)
            end_clock = time.fromisoformat(day_end)
        except ValueError:
            return "日期或时间格式错误，日期用 YYYY-MM-DD，时间用 HH:MM。"
        if end_clock <= start_clock:
            return "day_end 必须晚于 day_start。"
        ids = select_member_ids(members, members_query)
        if not ids:
            return "没有找到参与计算的成员。"
        slots = common_free_slots(members, target, ids, start_clock, end_clock, max(1, int(minimum_minutes)))
        labels = [str(members[user_id].get("name") or user_id) for user_id in ids]
        if not slots:
            return f"{target:%Y-%m-%d} 指定成员没有满足时长的共同空闲时间。"
        lines = [f"共同空闲时间（{', '.join(labels)}）："]
        lines.extend(f"{start:%H:%M}-{end:%H:%M}（{int((end-start).total_seconds()/60)} 分钟）" for start, end in slots)
        return "\n".join(lines)

    async def _shared_classes_text(
        self, event: AstrMessageEvent, target_date: str, members_query: str = ""
    ) -> str:
        members = await self._get_scope_members(event)
        try:
            target = date.fromisoformat(target_date)
        except ValueError:
            return "target_date 应使用 YYYY-MM-DD 格式。"
        ids = select_member_ids(members, members_query)
        grouped: dict[tuple[str, datetime, datetime], list[tuple[str, str]]] = {}
        for user_id, info, occurrence in day_occurrences(members, target, ids):
            course_key = re.sub(r"\s+", "", str(occurrence.get("SUMMARY") or "")).casefold()
            key = (course_key, occurrence["_start"], occurrence["_end"])
            grouped.setdefault(key, []).append((user_id, str(info.get("name") or user_id)))
        shared = [(key, people) for key, people in grouped.items() if len({user_id for user_id, _ in people}) >= 2]
        if not shared:
            return f"{target:%Y-%m-%d} 没有找到至少两人同一时间、同名的课程。"
        lines = [f"{target:%Y-%m-%d} 同一节课："]
        for (course, start, end), people in sorted(shared, key=lambda item: item[0][1]):
            names = sorted({name for _, name in people})
            lines.append(f"{start:%H:%M}-{end:%H:%M} {course}：{', '.join(names)}")
        return "\n".join(lines)

    async def _query_schedule_sql_text(
        self, event: AstrMessageEvent, sql: str, time_range: str = "today"
    ) -> str:
        members = await self._get_scope_members(event)
        if not members:
            return "当前会话还没有保存任何课程表。"
        names = {
            user_id: str(info.get("name") or user_id)
            for user_id, info in members.items()
            if isinstance(info, dict)
        }
        return await asyncio.to_thread(
            execute_course_schedule_sql,
            members,
            names,
            sql,
            time_range,
            datetime.now(LOCAL_TZ),
        )

    async def _group_schedule_image(
        self, event: AstrMessageEvent, target_date: date | None = None
    ) -> str | None:
        members = await self._get_scope_members(event)
        if not members:
            return None

        now = datetime.now(LOCAL_TZ)
        selected_date = target_date or now.date()
        start_bound, end_bound = _day_bounds(selected_date)
        rows: list[dict[str, str]] = []
        for user_id, info in members.items():
            if not isinstance(info, dict):
                continue
            for occurrence in _expand_member_occurrences(info, start_bound, end_bound):
                course = occurrence.get("SUMMARY") or "未命名课程"
                if occurrence.get("LOCATION"):
                    course += f" @ {occurrence['LOCATION']}"
                status = "当天"
                if selected_date == now.date() and occurrence["_start"] <= now < occurrence["_end"]:
                    status = "正在上"
                elif selected_date == now.date() and occurrence["_end"] <= now:
                    status = "已结束"
                rows.append(
                    {
                        "user_id": user_id,
                        "name": str(info.get("name") or user_id),
                        "subtitle": user_id,
                        "status": status,
                        "course": course,
                        "time": f"{occurrence['_start']:%H:%M}-{occurrence['_end']:%H:%M}",
                    }
                )

        rows.sort(key=lambda row: (row["time"], row["name"], row["course"]))
        return _draw_rows_image(
            f"课程表 {selected_date:%Y-%m-%d}", rows, f"schedule_{selected_date:%Y%m%d}.png"
        )

    async def _group_today_image(self, event: AstrMessageEvent) -> str | None:
        return await self._group_schedule_image(event)
