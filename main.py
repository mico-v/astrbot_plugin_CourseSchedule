from __future__ import annotations

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

from .plugin.constants import PLUGIN_ID
from .plugin.course_schedule import CourseScheduleBase


@register(PLUGIN_ID, "CourseSchedule", "保存并查询群友课程表", "0.6.0")
class CourseSchedulePlugin(CourseScheduleBase, Star):
    def __init__(self, context: Context):
        super().__init__(context)

    @filter.command("今日课表")
    async def today_schedule(self, event: AstrMessageEvent):
        """生成当前会话今日课程表图片"""
        path = await self._group_today_image(event)
        if not path:
            yield event.plain_result("当前会话还没有可展示的今日课程表。")
            return
        yield event.image_result(path)

    @filter.command("同步课表")
    async def sync_schedule_files(self, event: AstrMessageEvent):
        """按时间戳同步当前群的 .ics 课程表文件"""
        yield event.plain_result(await self._sync_group_files_text(event))

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
        """用 SQL 修改本地保存的结构化课程表，并自动更新本地 .ics 内容和本地时间戳。不会同步或上传群文件。

        可修改表：
        local_courses(id, course, location, description, dtstart, dtend, dtstart_tzid, dtend_tzid, rrule)

        仅支持一条 UPDATE、INSERT 或 DELETE。修改/删除已有课程时必须用 WHERE id=... 精确指定。
        dtstart/dtend 使用 iCalendar 时间格式，例如 20260526T090000。

        Args:
            sql(string): 修改 local_courses 的 SQL。不要包含分号。
            query(string): 成员 QQ 号或昵称关键字。留空表示发起人自己的课程表。
        """
        return await self._edit_local_schedule_sql_text(event, sql, query)


__all__ = ["CourseSchedulePlugin"]
