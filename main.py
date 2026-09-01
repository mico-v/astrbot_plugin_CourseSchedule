from __future__ import annotations

from datetime import date
import re

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.api.web import error_response, json_response, request

from .plugin.constants import PLUGIN_ID
from .plugin.course_schedule import CourseScheduleBase
from .plugin.message_files import extract_ics_from_event
from .plugin.sqlite_store import ScheduleWriteConflict


@register(PLUGIN_ID, "CourseSchedule", "保存并查询群友课程表", "0.8.1")
class CourseSchedulePlugin(CourseScheduleBase, Star):
    def __init__(self, context: Context):
        super().__init__(context)
        context.register_web_api(
            f"/{PLUGIN_ID}/scopes",
            self._web_scopes,
            ["GET"],
            "List course schedule scopes and members",
        )
        context.register_web_api(
            f"/{PLUGIN_ID}/schedule",
            self._web_schedule,
            ["GET"],
            "Get one member course schedule",
        )
        context.register_web_api(
            f"/{PLUGIN_ID}/schedule/save",
            self._web_save_schedule,
            ["POST"],
            "Save one member course schedule",
        )

    async def _web_scopes(self):
        return json_response(await self._page_scopes())

    async def _web_schedule(self):
        scope_id = str(request.query.get("scope_id") or "").strip()
        user_id = str(request.query.get("user_id") or "").strip()
        try:
            schedule = await self._page_schedule(scope_id, user_id)
        except ValueError as exc:
            return error_response(str(exc), status_code=400)
        if schedule is None:
            return error_response("找不到指定成员的课程表。", status_code=404)
        return json_response(schedule)

    async def _web_save_schedule(self):
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("请求体必须是 JSON 对象。", status_code=400)
        try:
            saved = await self._save_page_schedule(
                payload,
                actor=str(request.username or "webui"),
            )
        except ScheduleWriteConflict as exc:
            return error_response(str(exc), status_code=409)
        except ValueError as exc:
            return error_response(str(exc), status_code=400)
        return json_response(saved)

    @filter.command("今日课表")
    async def today_schedule(self, event: AstrMessageEvent):
        """生成当前会话今日课程表图片"""
        path = await self._group_today_image(event)
        if not path:
            yield event.plain_result("当前会话还没有可展示的今日课程表。")
            return
        yield event.image_result(path)

    @filter.command("课表")
    async def schedule(self, event: AstrMessageEvent, query: str = ""):
        """查询指定日期课程表，例如 /课表 或 /课表 2026-09-01。"""
        value = str(query or "").strip()
        if not value:
            try:
                getter = getattr(event, "get_message_str", None)
                raw = str(getter() if callable(getter) else getattr(event, "message_str", "") or "")
                parts = raw.strip().split(maxsplit=1)
                value = parts[1].strip() if len(parts) == 2 else ""
            except Exception:
                value = ""
        target = None
        if value:
            try:
                target = date.fromisoformat(value)
            except ValueError:
                yield event.plain_result("日期格式应为 YYYY-MM-DD，例如：/课表 2026-09-01")
                return
        path = await self._group_schedule_image(event, target)
        if not path:
            yield event.plain_result("当前会话还没有可展示的课程表。")
            return
        yield event.image_result(path)

    async def _import_ics_event(self, event: AstrMessageEvent):
        if getattr(event, "_course_schedule_ics_imported", False):
            return
        extracted = await extract_ics_from_event(event)
        if not extracted:
            yield event.plain_result(
                "未检测到 .ics 文件。请发送 /导入课表 并附加 .ics 文件，或直接发送 .ics 文件。"
            )
            return
        content, filename = extracted
        try:
            setattr(event, "_course_schedule_ics_imported", True)
        except Exception:
            pass
        # Preserve the established group-file convention: schedule<QQ>.ics
        # updates that member's row even when another user references the file.
        match = re.fullmatch(r"schedule(\d+)\.ics", filename.strip(), re.IGNORECASE)
        target_user_id = match.group(1) if match else None
        result = await self._save_ics_schedule(
            event,
            content,
            user_id=target_user_id,
            source_file=filename,
            uploader_id=event.get_sender_id(),
        )
        yield event.plain_result(result)

    @filter.command("导入课表")
    async def import_schedule(self, event: AstrMessageEvent):
        """导入当前消息附加的 .ics 文件并保存到 SQLite。"""
        async for response in self._import_ics_event(event):
            yield response

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_ics_message(self, event: AstrMessageEvent):
        """Automatically import an uploaded/referenced .ics message."""
        if getattr(event, "_course_schedule_ics_imported", False):
            return
        # The explicit command owns its response; avoid importing it twice when
        # the adapter dispatches every message to the generic handler as well.
        try:
            getter = getattr(event, "get_message_str", None)
            message_text = str(getter() if callable(getter) else getattr(event, "message_str", "") or "").strip()
        except Exception:
            message_text = ""
        if message_text.startswith("/"):
            return
        extracted = await extract_ics_from_event(event)
        if not extracted:
            return
        content, filename = extracted
        match = re.fullmatch(r"schedule(\d+)\.ics", filename.strip(), re.IGNORECASE)
        target_user_id = match.group(1) if match else None
        result = await self._save_ics_schedule(
            event,
            content,
            user_id=target_user_id,
            source_file=filename,
            uploader_id=event.get_sender_id(),
        )
        yield event.plain_result(result)

    @filter.llm_tool(name="query_course_schedule_sql")
    async def query_course_schedule_sql_tool(
        self, event: AstrMessageEvent, sql: str, time_range: str = "today"
    ):
        """用类似 SQL 的只读查询检索当前会话课程表，适合复杂查询、多人查询和统计。

        可查询表：
        members(user_id, name, source, updated_at, schedule_updated_at, source_file, event_count, schedule_text)
        courses(user_id, name, course, location, description, start_time, end_time, date, weekday, weekday_name, start_clock, end_clock, duration_minutes, status, source_file, rrule)

        Args:
            sql(string): 只读 SELECT 语句。不要包含分号。可按 user_id、name、date、weekday、course、location、status 等字段过滤、聚合和排序。
            time_range(string): 展开课程事件的时间范围，支持 today、tomorrow、yesterday、本周、下周、本月、YYYY-MM-DD 或 YYYY-MM-DD..YYYY-MM-DD。默认 today。
        """
        return await self._query_schedule_sql_text(event, sql, time_range)

    @filter.llm_tool(name="edit_local_course_schedule_sql")
    async def edit_local_course_schedule_sql_tool(
        self, event: AstrMessageEvent, sql: str, query: str = ""
    ):
        """用 SQL 修改本地保存的结构化课程表，并自动更新本地 .ics 内容和本地时间戳。不会执行网络操作。

        可修改表：
        local_courses(id, course, location, description, dtstart, dtend, dtstart_tzid, dtend_tzid, rrule)

        仅支持一条 UPDATE、INSERT 或 DELETE。修改/删除已有课程时必须用 WHERE id=... 精确指定。
        dtstart/dtend 使用 iCalendar 时间格式，例如 20260526T090000。

        Args:
            sql(string): 修改 local_courses 的 SQL。不要包含分号。
            query(string): 成员 QQ 号或昵称关键字。留空表示发起人自己的课程表。
        """
        return await self._edit_local_schedule_sql_text(event, sql, query)

    @filter.llm_tool(name="create_course")
    async def create_course_tool(
        self,
        event: AstrMessageEvent,
        course: str,
        start_time: str,
        end_time: str,
        location: str = "",
        description: str = "",
        rrule: str = "",
    ):
        """创建发起人的课程事件并保存到 SQLite，同时重建 ICS。"""
        return await self._create_course_text(
            event, course, start_time, end_time, location, description, rrule
        )

    @filter.llm_tool(name="update_course")
    async def update_course_tool(
        self,
        event: AstrMessageEvent,
        course_id: int,
        query: str = "",
        course: str = "",
        start_time: str = "",
        end_time: str = "",
        location: str = "",
        description: str = "",
        rrule: str = "",
    ):
        """按成员课程表中的 course_id 更新课程；留空字段保持原值。"""
        return await self._update_course_text(
            event,
            course_id,
            query,
            course=course,
            start_time=start_time,
            end_time=end_time,
            location=location,
            description=description,
            rrule=rrule,
        )

    @filter.llm_tool(name="delete_course")
    async def delete_course_tool(
        self, event: AstrMessageEvent, course_id: int, query: str = ""
    ):
        """按成员课程表中的 course_id 删除课程。"""
        return await self._delete_course_text(event, course_id, query)

    @filter.llm_tool(name="query_daily_course_schedule")
    async def query_daily_course_schedule_tool(
        self, event: AstrMessageEvent, target_date: str = "", members_query: str = ""
    ):
        """查询指定日期全部成员的课程，返回按开始时间排序的文本。"""
        return await self._daily_schedule_text(event, target_date, members_query)

    @filter.llm_tool(name="find_common_free_slots")
    async def find_common_free_slots_tool(
        self,
        event: AstrMessageEvent,
        target_date: str,
        members_query: str = "",
        day_start: str = "08:00",
        day_end: str = "22:00",
        minimum_minutes: int = 30,
    ):
        """查找成员在指定日期的共同空闲时间段。"""
        return await self._common_free_time_text(
            event, target_date, members_query, day_start, day_end, minimum_minutes
        )

    @filter.llm_tool(name="find_shared_classes")
    async def find_shared_classes_tool(
        self, event: AstrMessageEvent, target_date: str, members_query: str = ""
    ):
        """查找指定日期至少两人同时间、同课程名的课程。"""
        return await self._shared_classes_text(event, target_date, members_query)


__all__ = ["CourseSchedulePlugin"]
