# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Tests (unittest files run under pytest; venv already has the deps)
.venv/bin/python -m pytest tests -q
.venv/bin/python -m pytest tests/test_rank.py -q
.venv/bin/python -m pytest tests/test_rank.py::RankBoardTests::test_union_metric_counts_overlapping_courses_once -q

# Lint / format. ruff.toml configures the rule set; the tree is hand-formatted
# wider than ruff's formatter default, so `ruff format` is for new files only.
uvx ruff check .
```

There is no build step and no packaging: AstrBot loads the repository itself as the plugin package. To try it live, place/clone the repo at `AstrBot/data/plugins/astrbot_plugin_course_schedule` (the **directory name must stay `astrbot_plugin_course_schedule`** — `main.py` uses relative imports and `constants.PLUGIN_ID` matches it), install `requirements.txt`, then reload the plugin in the AstrBot WebUI. Chat I/O cannot be exercised from the tests; only the pure logic can.

## Architecture

**Layering.** `main.py` is the AstrBot surface only: `@register`ed class `CourseSchedulePlugin(CourseScheduleBase, Star)`, `@filter.command` handlers, the two `@filter.llm_tool` schema wrappers (`find`, `edit`), and five `register_web_api` routes. All behaviour lives in `plugin/`, imported as a flat module package, with `plugin/course_schedule.py::CourseScheduleBase` as the application service every entry point funnels through. Leaf modules (`domain`, `occurrences`, `ics`, `rank`, `day_off`, `sql_query`) hold pure logic and import nothing from AstrBot, which is why the tests can load them without an AstrBot install.

**Test harness.** AstrBot is not installed. Every test file starts with `from _plugin_loader import load_plugin_module` (from `tests/_plugin_loader.py`), which stubs `astrbot.*`, points the plugin data directory at a temp dir, and imports the one module the file needs — relative imports pull in the rest. Pass a unique `package_name` per test file so their module namespaces don't collide. Service tests build `CourseScheduleBase.__new__(CourseScheduleBase)` and inject `SQLiteScheduleStore(db_path=<tempdir>)` to bypass the real data-dir lookup.

**Storage.** SQLite (`data/plugin_data/astrbot_plugin_course_schedule/course_schedule.sqlite3`, WAL) with three tables: `schedule_members` (one JSON blob per member plus a `revision`), `course_events` (one row per VEVENT, ordered by `event_index`, deleted and reinserted wholesale on every write), and `schedule_day_overrides` (休假/调休 markers). Scope key is `group:<id>` or `private:<sender_id>` (`plugin/store.py`). `user_id = '*'` in the overrides table means "every member of the scope"; the store merges scope rows under member rows before attaching them as `info["_day_overrides"]`, so a member marker always wins. Every store method is an async wrapper around a `_*_sync` body serialized by one `asyncio.Lock`; writes run `BEGIN IMMEDIATE` with revision optimistic locking — `expected_revision=None` upserts, `0` is insert-only, `N` is compare-and-swap, and a mismatch raises `ScheduleWriteConflict` (mapped to HTTP 409 by the WebUI route).

**Event representation.** The internal canonical form is the dict produced by `plugin/ics.py::_component_to_event`: UPPERCASE iCalendar keys (`SUMMARY`, `DTSTART`, `DTEND`, `RRULE`, `LOCATION`, `DESCRIPTION`, `UID`, `DTSTAMP`, plus `*_TZID`) *and* `RAW_ICAL`, the original VEVENT text. `RAW_ICAL` is what preserves RDATE/EXDATE and any property the code doesn't model, so never drop it when rebuilding an event, and `_serialize_schedule_ics` reuses `base_ics` for the non-VEVENT parts of the calendar. Events are only ever read through **occurrences**: `plugin/occurrences.py::_expand_indexed_occurrences` expands RRULE (dateutil), RDATE/EXDATE, applies day overrides, and returns copies carrying tz-aware `_start`/`_end` (plus `_shifted_from` for 调休). `course_id` is the 1-based index into `info["events"]`, i.e. a position, not a stable identifier — every write re-sorts events by `DTSTART`, so indices move between calls and all callers must re-`find` before editing.

**Time.** `constants.LOCAL_TZ` is `Asia/Shanghai` and is used for every parse, display and day boundary; `_now_iso()` (UTC ISO) is only for bookkeeping fields. `_parse_ics_datetime_obj` parses both iCalendar text and ISO strings, and `domain.normalize_datetime` normalizes user input to `%Y%m%dT%H%M%S`.

**Date parsing has two shared entry points — reuse them, don't add a third.** `plugin/day_off.py` parses free-text *days* (relative words, `9.17`/`9月17日`, ranges, filler particles, `roll="nearest"` for `/课表` vs `roll="forward"` for marking future days). `plugin/time_range.py::_parse_time_range` parses *ranges* (今天/本周/上月/`YYYY-MM-DD..YYYY-MM-DD`) and is shared by `find`'s `time_range` and the rank board so both agree.

**Chat text binding gotcha.** AstrBot binds only the first word after a command name to the `query: str` handler parameter, so multi-argument tails (`2026-09-01 .. 2026-09-30`, `@小明`) only arrive via `texts._full_command_tail(event, query)`. Any new multi-arg command must read its tail that way. (`GreedyStr` from `astrbot.core.star.filter.command` would be the framework-native fix, but it is not in `astrbot.api` and a default value defeats it.)

**Permissions.** Commands, the `edit` tool and ICS import share one rule: acting on someone other than the sender requires a group scope and `event.is_admin()`. Private chats are self-only. A file named `schedule<QQ号>.ics` addresses that member's row, so importing one for someone else is an admin action (`main.py::_ics_filename_target` resolves the target, `_save_ics_schedule` enforces the check). `person` matches a QQ number or an *exact* nickname (no fuzzy match), and an unregistered nickname can only be resolved from an explicit @ mention in the current message (`_agent_mention_target`).

**Rendering** (`plugin/render.py`) is Pillow, always invoked through `asyncio.to_thread`, writing JPEG into `plugin_data/images` with a 24 h TTL prune. Text is drawn with the bundled Noto Sans CJK SC, falling back to Noto Color Emoji per grapheme cluster only when the CJK font lacks the glyph — so any measurement must go through `_rich_width`/`_fit_rich_text`/`_wrap_rich_text` rather than `font.getlength`, or emoji nicknames will break the layout. `_draw_rich_text` groups consecutive same-font clusters into one `draw.text` call; keep that batching, since per-glyph drawing is what made cards slow (see the timing guard in `tests/test_render.py`). Avatars are fetched over the network with a 3 s timeout and cached twice: a bounded in-memory dict with an explicit expiry (so a long-running process still honors the TTL) and one PNG per member on disk in `plugin_data/avatars`, pruned on each save. Failed downloads are cached for `AVATAR_FAILURE_TTL_SECONDS` only. The rank board (`plugin/rank.py`) defaults to the `union` metric (overlapping courses count once), clips occurrences to the window, and ignores DATE-only all-day events; `minutes` spans the whole window while `elapsed_minutes` is the finished part, which keeps a live week's board stable. `docs/rank-board-design.md` records the historical bugs this design exists to avoid.

**Day-image layout.** `daily_member_rows` tags each row with `is_live_day`, and `domain.split_folded_rows` turns that into `(cards, folded)`: on a live day the members with nothing left (`finished`/`none`/`holiday`) are drawn as a compact avatar grid by `render._draw_folded_members` instead of one full card each. Future and past days are never folded — there the countdown to the next class is what the reader wants — so the split is deliberately a no-op unless the day is today. The folded strip counts toward the subtitle total, and the legend only lists statuses a card actually shows.

**WebUI page** (`pages/schedule-manager/`) is plain static HTML/CSS/JS with no build step, talking to the plugin's web API through `window.AstrBotPluginPage.apiGet/apiPost` with plugin-relative paths; page titles come from `.astrbot-plugin/i18n/zh-CN.json`. The save flow sends the `revision` it read, and the server rejects stale writes — that is the intended UX, not an error.

**Conventions.** All user-facing strings, log messages and code comments are Chinese (replies) / English (code comments). Failures in chat flows are returned as strings to the caller, not raised.

**Stale docs.** `.github/copilot-instructions.md` and `.codex/project-memory.md` describe an older design (KV storage, OneBot group-file sync, `/课程表` sub-commands, SQL tools). They do not match the code — trust the code and `README.md` instead.
