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


@register(PLUGIN_ID, "CourseSchedule", "保存并查询群友课程表", "0.8.2")
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

    @filter.llm_tool(name="find")
    async def find_tool(
        self,
        event: AstrMessageEvent,
        person: str = "",
        time_range: str = "",
        field: str = "",
        value: str = "",
    ):
        """查找当前会话保存的课程表，支持按人、日期范围和字段组合筛选。

        不要拼接 SQL。person 只能使用 QQ 号或完整昵称精确匹配；不填表示发送消息的用户。
        如需查找全部成员，请显式传入 person=all、全部或所有。
        不填 time_range 表示查找全部已保存的课程事件；也支持 today、tomorrow、yesterday、
        本周、下周、本月、YYYY-MM-DD 和 YYYY-MM-DD..YYYY-MM-DD。指定日期范围时会展开 RRULE、
        RDATE 和 EXDATE，返回实际发生的课程。time_range=all、全部也表示全部事件。
        field/value 用于字段查找，支持 course、location、description、status、date、weekday、
        start_time、end_time、duration、rrule、member 和 user_id；文本字段支持包含匹配，
        status/date/weekday/user_id/member 使用精确匹配。返回的 course_id 可直接用于 edit。

        Args:
            person(string): QQ 号或完整昵称，精确查找某个人；留空表示发送消息的用户；查全部请填 all/全部。
            time_range(string): 时间范围；留空或填 all/全部查找全部已保存事件。
            field(string): 要筛选的字段，例如 course、location、status、date、member；留空不筛选。
            value(string): field 对应的值；使用 field 时必须填写。
        """
        return await self._find_schedule_text(event, person, time_range, field, value)

    @filter.llm_tool(name="edit")
    async def edit_tool(
        self,
        event: AstrMessageEvent,
        action: str,
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
    ):
        """统一新增、修改、删除课程，并自动维护 SQLite、本地 ICS 和查询数据。

        action 只能是 create/add、update/edit 或 delete/remove，也支持新增、修改、删除等中文。
        person 只能使用 QQ 号或完整昵称精确匹配；留空表示发起人自己。新增时如果目标成员还
        没有课表会自动创建；修改和删除必须先存在。群聊中修改其他人的课表仅管理员可用，
        管理员身份由消息事件校验，不能通过参数伪造；普通成员只能修改自己的课表。
        修改时 course_id 来自 find 结果，留空的课程字段保持原值。若要清空地点、备注或重复规则，
        将对应字段写入 clear_fields，例如 location,description 或 地点,备注。member_name 可在
        新增时设置新成员昵称，也可在修改时更新已有成员昵称。

        Args:
            action(string): 操作类型：create/add、update/edit、delete/remove，或新增/修改/删除。
            person(string): 目标 QQ 号或完整昵称；留空表示发起人自己。
            course_id(number): 修改或删除的课程编号，来自 find 返回的 course_id。
            course(string): 课程名称；新增必填，修改时留空保持原值。
            start_time(string): 开始时间，例如 2026-09-01 08:00；新增必填，修改时留空保持原值。
            end_time(string): 结束时间，例如 2026-09-01 09:30；新增必填，修改时留空保持原值。
            location(string): 上课地点；修改时留空保持原值，清空请使用 clear_fields。
            description(string): 课程备注；修改时留空保持原值，清空请使用 clear_fields。
            rrule(string): iCalendar 重复规则，例如 FREQ=WEEKLY;BYDAY=MO；没有重复则留空。
            member_name(string): 目标成员显示名称；新增时可用于创建新成员，修改时可重命名。
            clear_fields(string): 修改时要清空的字段，支持 location、description、rrule 及中文名称。
        """
        return await self._edit_schedule_text(
            event,
            action,
            person=person,
            course_id=course_id,
            course=course,
            start_time=start_time,
            end_time=end_time,
            location=location,
            description=description,
            rrule=rrule,
            member_name=member_name,
            clear_fields=clear_fields,
        )


__all__ = ["CourseSchedulePlugin"]
