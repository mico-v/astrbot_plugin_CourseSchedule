from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

from astrbot.api.event import AstrMessageEvent

from .constants import LOCAL_TZ, MAX_ICS_BYTES
from .ics import _format_ics_schedule, _parse_schedule_ics
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

    async def _group_today_image(self, event: AstrMessageEvent) -> str | None:
        members = await self._get_scope_members(event)
        if not members:
            return None

        now = datetime.now(LOCAL_TZ)
        start_bound, end_bound = _day_bounds(now.date())
        rows: list[dict[str, str]] = []
        for user_id, info in members.items():
            if not isinstance(info, dict):
                continue
            for occurrence in _expand_member_occurrences(info, start_bound, end_bound):
                course = occurrence.get("SUMMARY") or "未命名课程"
                if occurrence.get("LOCATION"):
                    course += f" @ {occurrence['LOCATION']}"
                status = "今天"
                if occurrence["_start"] <= now < occurrence["_end"]:
                    status = "正在上"
                elif occurrence["_end"] <= now:
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
        return _draw_rows_image(f"今日课程表 {now:%Y-%m-%d}", rows, "group_today.png")
