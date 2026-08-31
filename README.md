# astrbot_plugin_course_schedule

AstrBot 课程表插件，用于保存、查询和展示群友课程表。课程表以标准 iCalendar（`.ics`）作为导入/导出格式，本地使用 SQLite 持久化；支持文件消息自动导入及群文件引用后 @机器人触发导入。

## 功能

- 使用标准 `icalendar` 包解析和生成 `.ics`。
- 按会话作用域和 QQ 号保存每个人的课程表。
- 支持 RRULE、RDATE、EXDATE 等重复课程信息。
- 生成当前会话今日课程表图片。
- 向 AI 暴露只读 SQL 查询工具。
- 向 AI 暴露 SQL 修改工具，修改后自动重建本地 `.ics`。
- 图片渲染内置 Noto Sans CJK SC 中文字体和 Noto Emoji 字体，并支持按字符回退到系统 Emoji 字体。

今日课表图片按成员分卡片展示：正在上课、下一节即将上课、今日课程已结束和今日无课分别使用不同状态色；当天的当前课程会显示本节时长、距下课/上课倒计时和进度条。昵称中的 Unicode Emoji（包括国旗、肤色和 ZWJ 组合表情）会使用 Emoji 字体单独绘制，避免 CJK 字体缺字。

课程表数据保存于 AstrBot 的插件数据目录：

```text
data/plugin_data/astrbot_plugin_course_schedule/course_schedule.sqlite3
```

插件使用 SQLite 按“作用域 + QQ 号”保存成员记录，`VEVENT` 事件独立保存于 `course_events` 表；写入使用事务和 revision 乐观锁。

## ICS 导入

发送 `/导入课表` 并附加 `.ics` 文件即可导入。插件按 AstrBot 标准
`event.get_messages()` 消息链读取 `File` 消息段，并调用 `await File.get_file()` 获取本地文件；
独立的 `.ics` 文件消息也会交给自动导入处理器。
导入成功后会在当前会话作用域内按发送者 QQ 号写入 SQLite，并保留原始 ICS 内容。
插件不会调用 OneBot 群文件上传、下载或删除 API。

## 聊天命令

```text
/今日课表
/课表 [YYYY-MM-DD]
/导入课表 + .ics 附件
```

`/今日课表` 生成当前会话今日课程表图片。
`/课表 2026-09-01` 生成指定日期的课程表图片；不带日期时等同于今日。

## AI 工具调用

```text
query_course_schedule_sql(sql, time_range="today")
edit_local_course_schedule_sql(sql, query="")
```

另外提供面向课程领域的工具，Agent 无需拼接 SQL：

```text
create_course(course, start_time, end_time, location, description, rrule)
update_course(course_id, query, ...)
delete_course(course_id, query)
query_daily_course_schedule(target_date, members_query)
find_common_free_slots(target_date, members_query, day_start, day_end, minimum_minutes)
find_shared_classes(target_date, members_query)
```

其中 `course_id` 是该成员课表当前事件序号；修改工具会执行时间校验、乐观锁写入、
SQLite 更新和 ICS 重建。`find_common_free_slots` 会展开 RRULE/RDATE/EXDATE 后计算所有指定成员的时间交集。

工具均返回字符串给模型，不会直接向聊天窗口发送文本结果。

### SQL 查询工具

查询工具把当前会话的结构化事件展开到内存 SQLite，只允许执行一条 `SELECT` 查询。

可用表：

```text
members(user_id, name, source, updated_at, schedule_updated_at, source_file, event_count, schedule_text)
courses(user_id, name, course, location, description, start_time, end_time, date, weekday, weekday_name, start_clock, end_clock, duration_minutes, status, source_file, rrule)
```

`time_range` 支持 `today`、`tomorrow`、`yesterday`、本周、下周、本月、单个日期和日期范围。

### SQL 修改工具

修改工具只允许操作当前会话本地保存的结构化 ICS 课程：

```text
local_courses(id, course, location, description, dtstart, dtend, dtstart_tzid, dtend_tzid, rrule)
```

只支持一条 `UPDATE`、`INSERT` 或 `DELETE`。修改成功后会更新 SQLite 中的事件并重建本地 `.ics`，不会执行网络同步。

## 开发调试

1. 将本仓库放到 AstrBot 的 `data/plugins/astrbot_plugin_course_schedule` 目录下。
2. 安装插件依赖：`uv pip install -r requirements.txt`，或使用 AstrBot 插件管理器安装。
3. 在 AstrBot WebUI 插件管理中重载插件。
4. 发送 `/今日课表`，或用自然语言请求机器人查询/修改本地课程表。

## 参考

- [AstrBot 插件开发文档](https://docs.astrbot.app/dev/star/plugin-new.html)
- [AstrBot 存储文档](https://docs.astrbot.app/dev/star/guides/storage.html)
- [iCalendar RFC 5545](https://www.rfc-editor.org/rfc/rfc5545)
