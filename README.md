# astrbot_plugin_course_schedule

AstrBot 课程表插件，用于从 QQ 群文件中的 `.ics` 课程表同步数据，并在群聊中查看、列出和导出课程表。

## 功能

- 从群文件读取根目录 `schedule<QQ号>.ics`。
- 从群文件 `schedule/` 文件夹读取 `<QQ号>.ics`。
- 解析 iCalendar `.ics` 文件中的 `VEVENT` 课程事件。
- 按群号和 QQ 号保存每个人的课程表。
- 查看自己的课程表。
- 查看自己今天接下来要上的课程。
- 查看自己明天要上的课程。
- 在当前群聊中按昵称或 QQ 号查询他人的课程表。
- 生成群友当前/下一节课图片。
- 生成群友明日课程图片。
- 生成本周上课时长和节数排行榜图片。
- 图片渲染内置 Noto Sans CJK SC 中文字体。
- 列出当前会话已保存课程表的成员。
- 将自己的原始 `.ics` 上传回当前群文件。
- 手动保存文本课程表作为兜底。
- 删除自己的课程表。

课程表数据通过 AstrBot 插件 KV 存储保存，不写入插件源码目录。

## 群文件同步

把课程表文件上传到 QQ 群文件，支持两种放法。

放在群文件根目录：

```text
schedule<QQ号>.ics
```

放在群文件 `schedule` 文件夹：

```text
schedule/<QQ号>.ics
```

例如：

```text
schedule123456789.ics
schedule/123456789.ics
```

然后在群里发送：

```text
/课程表 同步群文件
```

插件会通过 OneBot 扩展 API 读取群文件列表和文件夹，下载符合命名规则的 `.ics` 文件，解析后按 QQ 号保存。

## 命令

```text
/课程表帮助
/课程表
/课程表 同步群文件
/课程表 导出
/查看课表
/查看明日课表
/群友在上什么课
/群友明天上什么课
/本周上课排行
/课程表 保存 <课程表内容>
/课程表 查看
/课程表 查看 <昵称或QQ号>
/课程表 列表
/课程表 删除
/课程表同步群文件
/课程表导出
/课程表保存 <课程表内容>
/课程表查看
/课程表查看 <昵称或QQ号>
/课程表列表
/课程表删除
```

## AI 工具调用

插件会向 AstrBot 注册以下 LLM tools，供模型在对话中按需查询已保存的课程表：

```text
query_course_schedule(query="")
query_course_schedule_day(day="today", query="")
list_course_schedules()
query_group_current_courses()
```

`query` 支持 QQ 号或昵称关键字，留空表示查询发起人自己。`day` 支持 `today`、`tomorrow`、
`今天`、`明天` 或 `YYYY-MM-DD`。按日期查询需要课程表来自 `.ics` 导入；手动文本保存的课程表会返回原始文本。

## 示例

```text
/课程表 同步群文件
```

```text
/课程表
/查看课表
/查看明日课表
/群友在上什么课
/群友明天上什么课
/本周上课排行
/课程表 查看 张三
/课程表 查看 123456789
/课程表 列表
```

## 平台要求

群文件读取和上传依赖 OneBot 实现的扩展 API：

- `get_group_root_files`
- `get_group_files_by_folder`
- `get_group_file_url`
- `upload_group_file`

这些 API 不是 OneBot 11 基础公开 API 的一部分，通常由 go-cqhttp、NapCat、Lagrange 等 OneBot v11 实现提供。

## 开发调试

1. 将本仓库放到 AstrBot 的 `data/plugins/astrbot_plugin_course_schedule` 目录下。
2. 使用 aiocqhttp / OneBot v11 平台，并确保协议端支持群文件 API。
3. 在 AstrBot WebUI 插件管理中重载插件。
4. 在群聊或私聊中发送命令测试。

## 参考

- [AstrBot 插件开发文档](https://docs.astrbot.app/dev/star/plugin-new.html)
- [AstrBot 存储文档](https://docs.astrbot.app/dev/star/guides/storage.html)
- [OneBot 11 公开 API](https://github.com/botuniverse/onebot-11/blob/master/api/public.md)
- [go-cqhttp 群文件 API](https://docs.go-cqhttp.org/api/)
