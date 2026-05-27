# astrbot_plugin_course_schedule

AstrBot 课程表插件，用于从 QQ 群文件中的 `.ics` 课程表同步数据，并在群聊中生成今日课程表图片。群文件同步通过指令触发；AI 通过 SQL 查询和修改本地结构化课程，插件自动更新本地 `.ics` 内容，后续同步指令按时间戳上传较新的本地版本。

## 功能

- 从群文件读取根目录旧格式 `schedule<QQ号>.ics`。
- 从群文件 `schedule/` 文件夹读取规范格式 `<QQ号>.ics`。
- 解析 iCalendar `.ics` 文件中的 `VEVENT` 课程事件。
- 按群号和 QQ 号保存每个人的课程表。
- 生成当前会话今日课程表图片。
- 通过指令按时间戳双向同步群文件课程表，上传时统一写入 `schedule/<QQ号>.ics`，成功后清理旧格式文件。
- 向 AI 暴露只读 SQL 查询工具，可查询不同人、不同时间范围的课程和统计结果。
- 向 AI 暴露 SQL 修改工具，可修改本地课程事件并自动重建本地 `.ics`。
- 图片渲染内置 Noto Sans CJK SC 中文字体。

课程表数据通过 AstrBot 插件 KV 存储保存，不写入插件源码目录。

## 群文件同步

把课程表文件上传到 QQ 群文件，推荐使用规范路径。

规范路径：

```text
schedule/<QQ号>.ics
```

为了兼容旧文件，插件同步时仍会读取旧格式：

群文件根目录：

```text
schedule<QQ号>.ics
```

例如：

```text
schedule123456789.ics
schedule/123456789.ics
```

同步操作通过聊天指令触发：

```text
/同步课表
```

插件会通过 OneBot 扩展 API 读取群文件列表和文件夹，下载符合命名规则的 `.ics` 文件。同步按时间戳比较：

- 远端较新：下载群文件并覆盖本地缓存。
- 本地较新：上传本地 `.ics` 到 `schedule/<QQ号>.ics`。
- 时间相同：规范路径已存在时只更新时间戳记录并跳过；仍是旧路径时会上传规范路径。

上传规范路径成功后，插件会重新扫描群文件，并删除同一 QQ 号对应的旧格式文件或重复文件。旧文件删除失败只会计入同步结果，不会回滚已经成功的上传。

AI 通过 SQL 修改本地课程后会刷新本地 `schedule_updated_at` 并重建本地 `.ics`，此时再执行 `/同步课表` 会把本地较新的版本上传到群文件。

## 聊天命令

插件保留两个面向聊天的指令：

```text
/今日课表
/同步课表
```

`/今日课表` 生成当前会话今日课程表图片。`/同步课表` 按时间戳同步当前群的 `.ics` 课程表文件。

## AI 工具调用

插件向 AstrBot 注册以下 LLM tools。工具均返回字符串给模型，不会直接向聊天窗口发送文本结果。

```text
query_course_schedule_sql(sql, time_range="today")
edit_local_course_schedule_sql(sql, query="")
```

AI 工具不负责同步、上传群文件，也不直接操作 `.ics` 文件文本。修改流程是：模型先用查询 SQL 找到要改的课程，再用修改 SQL 更新本地课程事件；插件会自动重建本地 `.ics`，用户再发送 `/同步课表` 上传本地较新的版本。

### SQL 查询工具

`query_course_schedule_sql` 会把指定时间范围内展开后的课程事件放入内存 SQLite，只允许执行一条 `SELECT` 查询。

可用表：

```text
members(user_id, name, source, updated_at, schedule_updated_at, source_file, event_count, schedule_text)
courses(user_id, name, course, location, description, start_time, end_time, date, weekday, weekday_name, start_clock, end_clock, duration_minutes, status, source_file, rrule)
```

`time_range` 支持：

```text
today
tomorrow
yesterday
本周
下周
本月
YYYY-MM-DD
YYYY-MM-DD..YYYY-MM-DD
```

查询示例：

```sql
SELECT name, start_clock, end_clock, course, location
FROM courses
WHERE date = '2026-05-26'
ORDER BY start_time
```

```sql
SELECT name, COUNT(*) AS course_count, ROUND(SUM(duration_minutes) / 60.0, 1) AS hours
FROM courses
GROUP BY user_id, name
ORDER BY hours DESC
```

```sql
SELECT name, course, start_time, end_time
FROM courses
WHERE name LIKE '%张三%' AND status IN ('current', 'future')
ORDER BY start_time
LIMIT 10
```

### SQL 修改工具

`edit_local_course_schedule_sql` 只允许修改当前会话本地保存的结构化 `.ics` 课程表，不会同步或上传群文件。

可修改表：

```text
local_courses(id, course, location, description, dtstart, dtend, dtstart_tzid, dtend_tzid, rrule)
```

限制：

- 只支持一条 `UPDATE`、`INSERT` 或 `DELETE`。
- 修改或删除已有课程时使用 `WHERE id=...` 精确指定。
- `dtstart` / `dtend` 使用 iCalendar 时间格式，例如 `20260526T090000`。
- 修改成功后会重建本地 `.ics`，刷新本地时间戳，但不操作群文件。

修改示例：

```sql
UPDATE local_courses SET course='高等数学', location='A101' WHERE id=2
```

```sql
INSERT INTO local_courses(course, location, dtstart, dtend)
VALUES ('高等数学', 'A101', '20260526T090000', '20260526T103000')
```

```sql
DELETE FROM local_courses WHERE id=3
```

## 平台要求

群文件读取和上传依赖 OneBot 实现的扩展 API：

- `get_group_root_files`
- `get_group_files_by_folder`
- `get_group_file_url`
- `upload_group_file`
- `create_group_file_folder`
- `delete_group_file`

这些 API 不是 OneBot 11 基础公开 API 的一部分，通常由 go-cqhttp、NapCat、Lagrange 等 OneBot v11 实现提供。

## 开发调试

1. 将本仓库放到 AstrBot 的 `data/plugins/astrbot_plugin_course_schedule` 目录下。
2. 使用 aiocqhttp / OneBot v11 平台，并确保协议端支持群文件 API。
3. 在 AstrBot WebUI 插件管理中重载插件。
4. 在群聊或私聊中发送 `/今日课表` 测试图片生成，发送 `/同步课表` 测试群文件同步，或用自然语言请求机器人查询/修改本地课程表。

## 参考

- [AstrBot 插件开发文档](https://docs.astrbot.app/dev/star/plugin-new.html)
- [AstrBot 存储文档](https://docs.astrbot.app/dev/star/guides/storage.html)
- [OneBot 11 公开 API](https://github.com/botuniverse/onebot-11/blob/master/api/public.md)
- [go-cqhttp 群文件 API](https://docs.go-cqhttp.org/api/)
