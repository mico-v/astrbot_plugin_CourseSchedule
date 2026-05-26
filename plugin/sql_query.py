from __future__ import annotations

import re
import sqlite3
from datetime import date, datetime, time, timedelta
from typing import Any

from .constants import LOCAL_TZ
from .occurrences import _duration_hours, _expand_member_occurrences

MAX_SQL_RESULT_ROWS = 100
MAX_CELL_CHARS = 120

_FORBIDDEN_SQL_RE = re.compile(
    r"\b(attach|alter|create|delete|detach|drop|insert|pragma|replace|update|vacuum)\b",
    re.IGNORECASE,
)
_RANGE_SPLIT_RE = re.compile(r"\s*(?:\.\.|~|至|到|—|-{2,}|\bto\b)\s*", re.IGNORECASE)


def _schema_help() -> str:
    return "\n".join(
        [
            "请传入只读 SELECT 查询。可用表：",
            "members(user_id, name, source, updated_at, schedule_updated_at, source_file, event_count, schedule_text)",
            "courses(user_id, name, course, location, description, start_time, end_time, date, weekday, weekday_name, start_clock, end_clock, duration_minutes, status, source_file, rrule)",
            "",
            "时间字段均为 Asia/Shanghai 本地时间文本。status 可取 past、current、future。",
            "示例：",
            "SELECT name, start_clock, end_clock, course, location FROM courses WHERE date='2026-05-26' ORDER BY start_time",
            "SELECT name, COUNT(*) AS course_count, ROUND(SUM(duration_minutes)/60.0, 1) AS hours FROM courses GROUP BY user_id, name ORDER BY hours DESC",
        ]
    )


def _month_bounds(day: date, offset: int = 0) -> tuple[date, date]:
    month_index = day.month - 1 + offset
    year = day.year + month_index // 12
    month = month_index % 12 + 1
    start = date(year, month, 1)
    next_month_index = month_index + 1
    next_year = day.year + next_month_index // 12
    next_month = next_month_index % 12 + 1
    end = date(next_year, next_month, 1) - timedelta(days=1)
    return start, end


def _parse_date_token(token: str, today: date) -> date:
    normalized = str(token or "").strip().lower()
    if normalized in {"", "today", "今天", "今日"}:
        return today
    if normalized in {"tomorrow", "明天", "明日"}:
        return today + timedelta(days=1)
    if normalized in {"yesterday", "昨天", "昨日"}:
        return today - timedelta(days=1)
    return date.fromisoformat(normalized)


def _parse_sql_time_range(
    value: str, today: date | None = None
) -> tuple[datetime, datetime, str]:
    today = today or datetime.now(LOCAL_TZ).date()
    normalized = str(value or "today").strip().lower()
    compact = re.sub(r"\s+", "", normalized)

    if compact in {"", "today", "今天", "今日"}:
        start_date = end_date = today
    elif compact in {"tomorrow", "明天", "明日"}:
        start_date = end_date = today + timedelta(days=1)
    elif compact in {"yesterday", "昨天", "昨日"}:
        start_date = end_date = today - timedelta(days=1)
    elif compact in {"thisweek", "currentweek", "week", "本周", "这周", "这一周"}:
        start_date = today - timedelta(days=today.weekday())
        end_date = start_date + timedelta(days=6)
    elif compact in {"nextweek", "下周", "下一周"}:
        start_date = today - timedelta(days=today.weekday()) + timedelta(days=7)
        end_date = start_date + timedelta(days=6)
    elif compact in {"thismonth", "currentmonth", "month", "本月", "这个月"}:
        start_date, end_date = _month_bounds(today)
    elif compact in {"nextmonth", "下月", "下个月"}:
        start_date, end_date = _month_bounds(today, 1)
    else:
        parts = [part for part in _RANGE_SPLIT_RE.split(normalized, maxsplit=1) if part]
        if len(parts) == 2:
            start_date = _parse_date_token(parts[0], today)
            end_date = _parse_date_token(parts[1], today)
        else:
            start_date = end_date = _parse_date_token(normalized, today)

    if end_date < start_date:
        raise ValueError("time_range 的结束日期不能早于开始日期。")

    start_bound = datetime.combine(start_date, time.min, tzinfo=LOCAL_TZ)
    end_bound = datetime.combine(end_date + timedelta(days=1), time.min, tzinfo=LOCAL_TZ)
    if start_date == end_date:
        label = f"{start_date:%Y-%m-%d}"
    else:
        label = f"{start_date:%Y-%m-%d}..{end_date:%Y-%m-%d}"
    return start_bound, end_bound, label


def _validate_sql(sql: str) -> str:
    query = str(sql or "").strip()
    if not query:
        raise ValueError(_schema_help())
    if not re.match(r"^select\b", query, flags=re.IGNORECASE):
        raise ValueError("只支持 SELECT 查询。\n\n" + _schema_help())
    if ";" in query:
        raise ValueError("一次只能执行一条 SELECT 查询，不要包含分号。")
    if _FORBIDDEN_SQL_RE.search(query):
        raise ValueError("只支持只读查询，不能包含写入、DDL、PRAGMA 或附件数据库语句。")
    return query


def _set_readonly_authorizer(conn: sqlite3.Connection) -> None:
    allowed_actions = {
        sqlite3.SQLITE_FUNCTION,
        sqlite3.SQLITE_READ,
        sqlite3.SQLITE_SELECT,
    }
    if hasattr(sqlite3, "SQLITE_RECURSIVE"):
        allowed_actions.add(sqlite3.SQLITE_RECURSIVE)

    def authorizer(action: int, _arg1: str, _arg2: str, _db: str, _source: str) -> int:
        return sqlite3.SQLITE_OK if action in allowed_actions else sqlite3.SQLITE_DENY

    conn.set_authorizer(authorizer)


def _create_db(
    members: dict[str, Any],
    names: dict[str, str],
    start_bound: datetime,
    end_bound: datetime,
    now: datetime,
) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE members (
            user_id TEXT,
            name TEXT,
            source TEXT,
            updated_at TEXT,
            schedule_updated_at TEXT,
            source_file TEXT,
            event_count INTEGER,
            schedule_text TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE courses (
            user_id TEXT,
            name TEXT,
            course TEXT,
            location TEXT,
            description TEXT,
            start_time TEXT,
            end_time TEXT,
            date TEXT,
            weekday INTEGER,
            weekday_name TEXT,
            start_clock TEXT,
            end_clock TEXT,
            duration_minutes INTEGER,
            status TEXT,
            source_file TEXT,
            rrule TEXT
        )
        """
    )

    weekday_names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    for user_id, info in members.items():
        if not isinstance(info, dict):
            continue

        name = names.get(user_id) or str(info.get("name") or user_id)
        conn.execute(
            """
            INSERT INTO members VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                name,
                str(info.get("source") or ""),
                str(info.get("updated_at") or ""),
                str(info.get("schedule_updated_at") or info.get("content_updated_at") or ""),
                str(info.get("source_file") or ""),
                int(info.get("event_count") or 0),
                str(info.get("schedule") or ""),
            ),
        )

        occurrences = _expand_member_occurrences(info, start_bound, end_bound)
        for occurrence in occurrences:
            start: datetime = occurrence["_start"]
            end: datetime = occurrence["_end"]
            status = "current" if start <= now < end else "future" if start > now else "past"
            conn.execute(
                """
                INSERT INTO courses VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    name,
                    str(occurrence.get("SUMMARY") or "未命名课程"),
                    str(occurrence.get("LOCATION") or ""),
                    str(occurrence.get("DESCRIPTION") or ""),
                    f"{start:%Y-%m-%d %H:%M}",
                    f"{end:%Y-%m-%d %H:%M}",
                    f"{start:%Y-%m-%d}",
                    start.weekday() + 1,
                    weekday_names[start.weekday()],
                    f"{start:%H:%M}",
                    f"{end:%H:%M}",
                    round(_duration_hours([occurrence]) * 60),
                    status,
                    str(info.get("source_file") or ""),
                    str(occurrence.get("RRULE") or ""),
                ),
            )

    _set_readonly_authorizer(conn)
    return conn


def _format_cell(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).replace("\n", " ").strip()
    if len(text) > MAX_CELL_CHARS:
        return text[: MAX_CELL_CHARS - 3] + "..."
    return text


def _format_rows(columns: list[str], rows: list[sqlite3.Row], truncated: bool) -> str:
    if not rows:
        return "SQL 查询完成，结果为空。"

    lines = [f"SQL 查询完成，返回 {len(rows)} 行" + ("（已截断）：" if truncated else "：")]
    lines.append(" | ".join(columns))
    lines.append(" | ".join("---" for _ in columns))
    for row in rows:
        lines.append(" | ".join(_format_cell(row[column]) for column in columns))
    if truncated:
        lines.append(f"结果超过 {MAX_SQL_RESULT_ROWS} 行，请在 SQL 中增加 WHERE 或 LIMIT。")
    return "\n".join(lines)


def execute_course_schedule_sql(
    members: dict[str, Any],
    names: dict[str, str],
    sql: str,
    time_range: str = "today",
    now: datetime | None = None,
) -> str:
    now = now or datetime.now(LOCAL_TZ)
    try:
        query = _validate_sql(sql)
        start_bound, end_bound, range_label = _parse_sql_time_range(time_range, now.date())
    except ValueError as exc:
        return str(exc)

    conn = _create_db(members, names, start_bound, end_bound, now)
    try:
        cursor = conn.execute(query)
        rows = cursor.fetchmany(MAX_SQL_RESULT_ROWS + 1)
        truncated = len(rows) > MAX_SQL_RESULT_ROWS
        rows = rows[:MAX_SQL_RESULT_ROWS]
        columns = [item[0] for item in cursor.description or []]
    except sqlite3.Error as exc:
        return f"SQL 查询失败：{exc}\n\n{_schema_help()}"
    finally:
        conn.close()

    body = _format_rows(columns, rows, truncated)
    return f"查询展开时间范围：{range_label}\n{body}"
