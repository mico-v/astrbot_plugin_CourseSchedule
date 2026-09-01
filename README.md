# astrbot_plugin_course_schedule

AstrBot 课程表插件，用于保存、查询和展示群友课程表。课程表以标准 iCalendar（`.ics`）作为导入/导出格式，本地使用 SQLite 持久化；支持文件消息自动导入及群文件引用后 @机器人触发导入。

## 功能

- 使用标准 `icalendar` 包解析和生成 `.ics`。
- 按会话作用域和 QQ 号保存每个人的课程表。
- 支持 RRULE、RDATE、EXDATE 等重复课程信息。
- 生成当前会话今日课程表图片。
- 在 AstrBot WebUI 的插件 Pages 中按群组、成员手动增删改课程，并同步保存到本地 ICS。
- 向 AI 暴露聚合的 `find` 查询工具和 `edit` 编辑工具，避免 Agent 在多个重复工具之间选择。
- 图片渲染统一使用内置 Noto Sans CJK SC 字体绘制中文、英文、数字和普通符号；仅当该字体确实缺少 Emoji 字形时，才使用单一 Emoji 字体兜底。

今日课表图片按成员分卡片展示：正在上课、下一节即将上课、今日课程已结束和今日无课分别使用不同状态色；当天的当前课程会显示本节时长、距下课/上课倒计时和进度条。昵称中的 Unicode Emoji（包括国旗、肤色和 ZWJ 组合表情）会使用 Emoji 字体单独绘制，避免 CJK 字体缺字。

在 AstrBot WebUI 打开本插件详情页中的“课表管理” Page，即可选择已保存课表的群组和成员，编辑显示名称、课程名称、起止时间、地点、备注和重复规则。保存时会校验课程时间并使用 revision 防止覆盖其他操作；若页面提示课表已更新，请刷新后再保存。

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

插件只向 Agent 暴露两个聚合工具：

```text
find(person="", time_range="", field="", value="")
edit(action, person="", course_id=0, course="", start_time="", end_time="", location="", description="", rrule="", member_name="", clear_fields="")
```

`find` 不需要拼接 SQL：

- `person` 使用 QQ 号或完整昵称精确查找；留空表示发送消息的用户；查找全部成员时显式传入 `all`、`全部` 或 `所有`。
- `time_range` 留空、`all` 或 `全部` 查找全部已保存的课程事件；也支持今天、明天、本周、
  本月、单个日期、`YYYY-MM-DD..YYYY-MM-DD` 日期范围和完整日期时间范围。指定范围时会展开
  RRULE、RDATE、EXDATE。
- `field/value` 可按课程名、地点、备注、状态、日期、星期、成员、QQ 号、时长或重复规则筛选。
  课程名、地点、备注和重复规则支持包含匹配，成员、状态、日期、星期、QQ 号使用精确匹配。
- 每条结果包含成员、时间、状态、时长和 `course_id`；`course_id` 可以直接交给 `edit`。

`edit` 的 `action` 支持 `create/add`、`update/edit`、`delete/remove`，也支持新增、修改、删除等中文。
新增课程必须填写 `course`、`start_time` 和 `end_time`；新增目标没有课表时会自动创建成员记录。
修改和删除使用 `find` 返回的 `course_id`，留空的修改字段保持原值；通过 `clear_fields` 可以清空
地点、备注或重复规则。`member_name` 可设置或修改成员昵称。

私聊只能编辑自己的课表。群聊中普通成员只能编辑自己的课表；管理员可通过同一个 `edit` 工具指定
其他成员的 QQ 号或完整昵称进行增删改，管理员身份由 AstrBot 消息事件校验。所有编辑都会执行时间
校验、revision 乐观锁写入、SQLite 更新和 ICS 重建。

工具均返回字符串给模型，不会直接向聊天窗口发送文本结果。

## 开发调试

1. 将本仓库放到 AstrBot 的 `data/plugins/astrbot_plugin_course_schedule` 目录下。
2. 安装插件依赖：`uv pip install -r requirements.txt`，或使用 AstrBot 插件管理器安装。
3. 在 AstrBot WebUI 插件管理中重载插件。
4. 发送 `/今日课表`，或用自然语言请求机器人查询/修改本地课程表。

## 参考

- [AstrBot 插件开发文档](https://docs.astrbot.app/dev/star/plugin-new.html)
- [AstrBot 存储文档](https://docs.astrbot.app/dev/star/guides/storage.html)
- [iCalendar RFC 5545](https://www.rfc-editor.org/rfc/rfc5545)
