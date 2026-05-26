from __future__ import annotations

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

from .plugin.constants import PLUGIN_ID
from .plugin.course_schedule import CourseScheduleBase
from .plugin.texts import _help_text, _strip_command


@register(PLUGIN_ID, "CourseSchedule", "保存并查询群友课程表", "0.3.0")
class CourseSchedulePlugin(CourseScheduleBase, Star):
    def __init__(self, context: Context):
        super().__init__(context)

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
        yield event.plain_result(
            await self._member_day_schedule_text(event, "", "tomorrow")
        )

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


__all__ = ["CourseSchedulePlugin"]
