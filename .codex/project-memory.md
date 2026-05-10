# Project Memory: astrbot_plugin_CourseSchedule

## Goal

Develop an AstrBot course schedule plugin for group chats. The preferred workflow is to sync group members' schedules from QQ group files named `schedule<qq号>.ics`, parse iCalendar events, save them by group/user, and output/query them on demand.

## Current Repository State

- This repository is already an AstrBot plugin template-style repo.
- Core files currently present:
  - `main.py`
  - `metadata.yaml`
  - `README.md`
- The template Hello World code has been replaced with a first course schedule MVP.
- Plugin identity is `astrbot_plugin_course_schedule`.
- Current command surface:
  - `/课程表`
  - `/课程表 同步群文件`
  - `/课程表 导出`
  - `/查看课表`
  - `/查看明日课表`
  - `/群友在上什么课`
  - `/群友明天上什么课`
  - `/本周上课排行`
  - `/课程表 保存 <课程表内容>`
  - `/课程表 查看 [昵称或QQ号]`
  - `/课程表 列表`
  - `/课程表 删除`
  - `/课程表 帮助`
  - Compatibility commands: `/课程表同步群文件`, `/课程表导出`, `/课程表保存`, `/课程表查看`, `/课程表列表`, `/课程表删除`, `/课程表帮助`
- Manual text save remains as a fallback, but `.ics` group file sync is the primary path.
- `.ics` sync implementation:
  - Lists group files through OneBot extension APIs.
  - Supports root files named `schedule<QQ号>.ics`.
  - Supports files under a `schedule/` group-file folder named `<QQ号>.ics`.
  - Downloads the group file URL.
  - Parses `VEVENT` fields: `SUMMARY`, `DTSTART`, `DTEND`, `LOCATION`, `DESCRIPTION`, `RRULE`.
  - Stores parsed events, formatted schedule text, source filename, and original `.ics` content.
- Schedule query features:
  - Expands weekly `RRULE` events for day/week windows.
  - Uses `Asia/Shanghai` as local query timezone.
  - Generates PNG summaries with Pillow for group current/next courses, tomorrow courses, and weekly rankings.
  - Bundled CJK fonts live in `assets/fonts/` and should be used before system fonts.
  - Avatar image source is QQ qlogo by user id, with a local text fallback.
- Export implementation:
  - Uploads the user's stored original `.ics` to the current group as `schedule<sender_id>.ics`.

## AstrBot Plugin Initialization Notes

- Official development docs: https://docs.astrbot.app/dev/star/plugin-new.html
- Recommended plugin repo name format:
  - Starts with `astrbot_plugin_`.
  - No spaces.
  - Lowercase letters.
  - Keep short.
- Normal local development layout is inside AstrBot core:
  - Clone AstrBot.
  - Create `AstrBot/data/plugins`.
  - Place/clone this plugin repo under `AstrBot/data/plugins/<plugin_name>`.
  - Open the AstrBot project in VS Code and edit the plugin directory there.
- AstrBot identifies plugin metadata from `metadata.yaml`; keep this file accurate.

## Required Plugin Metadata Work

Before real implementation, replace template values in `metadata.yaml`:

- `name`: should be this plugin's unique id, preferably `astrbot_plugin_course_schedule`.
- `display_name`: human-readable name, for example `Course Schedule` or `课程表`.
- `desc`: short description of saving and querying course schedules for group members.
- `version`: use AstrBot-style version strings such as `v0.1.0`.
- `author`: set to actual author.
- `repo`: set to actual repository URL when known.
- Optional:
  - `short_desc`: one-line market card description.
  - `support_platforms`: declare supported adapters if known.
  - `astrbot_version`: use PEP 440 style constraints without `v`, for example `>=4.9.2`, if relying on newer APIs.
  - `logo.png`: optional square logo, recommended 256x256.

## AstrBot Plugin Coding Conventions

- Main plugin class should inherit from `astrbot.api.star.Star`.
- Register plugin class with `@register(...)`.
- Command handlers should be methods inside the plugin class.
- Use `@filter.command("<command>")` for slash commands.
- Handler docstrings are useful because AstrBot parses them for user-facing command descriptions.
- Use `event.plain_result(...)` for plain text command replies.
- Use AstrBot's `logger` instead of ad hoc print logging.
- Implement `initialize()` and `terminate()` only when needed.
- Keep error handling robust; one bad schedule input should not crash the plugin.

## Storage Rules

- Official storage docs: https://docs.astrbot.app/dev/star/guides/storage.html
- Do not persist user/group schedule data inside the plugin source directory, because plugin updates or reinstalls can overwrite it.
- For small plugin-scoped data, AstrBot supports KV helpers on `Star`:
  - `await self.put_kv_data(key, value)`
  - `await self.get_kv_data(key, default)`
  - `await self.delete_kv_data(key)`
- For larger files, store under AstrBot data:
  - `data/plugin_data/{plugin_name}/`
  - Get the base path with `get_astrbot_data_path()` from `astrbot.core.utils.astrbot_path`.
- Course schedules are persistent user/group data, so prefer AstrBot data storage, not files beside `main.py`.
- Current implementation uses AstrBot KV storage under key `course_schedule:v1`.
- Saved members are scoped by `group:<group_id>` for groups and `private:<sender_id>` for private chats.

## OneBot Group File Notes

- OneBot 11 base public API docs: https://github.com/botuniverse/onebot-11/blob/master/api/public.md
- Group file operations are implementation extensions, not part of the base public API.
- Current implementation expects aiocqhttp / OneBot v11 via AstrBot and calls `event.bot.api.call_action(...)`.
- Required extension APIs:
  - `get_group_root_files`
  - `get_group_files_by_folder`
  - `get_group_file_url`
  - `upload_group_file`
- These APIs are commonly provided by go-cqhttp, NapCat, and Lagrange-style OneBot implementations, but adapter behavior can differ.

## Configuration And Dependencies

- If plugin behavior needs admin customization, add `_conf_schema.json`; AstrBot will generate and manage config under `data/config/<plugin_name>_config.json`.
- If third-party dependencies are needed, add `requirements.txt` in the plugin directory.
- Avoid `requests` for network calls; use async clients such as `aiohttp` or `httpx`.
- Do not add dependencies unless the feature clearly needs them.

## Debugging Workflow

- AstrBot loads plugins at runtime, so debugging requires running AstrBot core.
- After code changes, reload the plugin in AstrBot WebUI plugin management.
- If plugin loading fails, use the WebUI reload/repair path and inspect logs.
- Local hot-debug setup in this codespace:
  - AstrBot core cloned to `/workspaces/AstrBot`.
  - Plugin symlinked at `/workspaces/AstrBot/data/plugins/astrbot_plugin_course_schedule`.
  - AstrBot dependencies installed with `python -m pip install -e /workspaces/AstrBot`.
  - Start AstrBot with `cd /workspaces/AstrBot && python main.py`.
  - WebUI default URL from smoke test: `http://localhost:6185`.
  - Default WebUI credentials shown by AstrBot: `astrbot / astrbot`.
  - Smoke test confirmed plugin loading: AstrBot log showed `Loading plugin astrbot_plugin_course_schedule ...`.

## Quality Bar For This Project

- Run formatting before commits; AstrBot docs recommend `ruff`.
- Add focused tests or at least isolated validation helpers for schedule parsing and storage serialization when implementing behavior.
- Keep implementation compatible with the current AstrBot plugin template style unless there is a clear reason to deviate.
