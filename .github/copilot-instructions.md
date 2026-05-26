# Copilot instructions for astrbot_plugin_course_schedule

## Build, test, lint
- No repo-provided build, test, or lint commands/configs are present.
- Manual validation follows README "开发调试": place this repo under `AstrBot/data/plugins/astrbot_plugin_course_schedule`, reload the plugin in AstrBot WebUI, then exercise the slash commands.

## High-level architecture
- Single entrypoint `main.py` defines helper functions and `CourseSchedulePlugin`, registered via `@register(...)` from `astrbot.api.star`.
- Group file sync uses OneBot v11 extension APIs (`get_group_root_files`, `get_group_files_by_folder`, `get_group_file_url`, `upload_group_file`) to download `.ics` files named `schedule<QQ号>.ics` or `schedule/<QQ号>.ics`, then parses `VEVENT` data (with RRULE expansion) into structured events plus a formatted text schedule.
- Persistent data is stored in AstrBot KV storage under key `course_schedule:v1`, scoped by `group:<group_id>` or `private:<sender_id>`, with per-member records containing events, raw `.ics`, and timestamps.
- Image outputs (current/next class, tomorrow summary, weekly rank) are rendered with Pillow; fonts are loaded from `assets/fonts` before falling back to system fonts.

## Key conventions
- Command handlers live on the plugin class and use `@filter.command(...)`; the main command is `/课程表` with sub-actions parsed from the message text.
- Use `event.plain_result(...)` for text responses and `event.image_result(...)` for generated PNGs.
- Log failures with AstrBot `logger` rather than `print`.
- Time handling uses `Asia/Shanghai` as `LOCAL_TZ` for display/queries and converts to UTC for storage.
- LLM tools are registered with `@filter.llm_tool` (`query_course_schedule`, `query_course_schedule_day`, `list_course_schedules`, `query_group_current_courses`).
