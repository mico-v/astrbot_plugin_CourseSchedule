from __future__ import annotations

from datetime import date, datetime, timedelta

from .constants import LOCAL_TZ


def _format_time(value: str | None) -> str:
    if not value:
        return "未知时间"

    return value.replace("T", " ").replace("+00:00", " UTC")


def _is_own_query(query: str) -> bool:
    return str(query or "").strip().lower() in {"", "我", "自己", "本人", "me", "self"}


def _parse_date_query(day: str) -> tuple[date | None, str]:
    normalized = str(day or "").strip().lower()
    today = datetime.now(LOCAL_TZ).date()
    if normalized in {"", "today", "今天", "今日"}:
        return today, "今天"
    if normalized in {"tomorrow", "明天", "明日"}:
        return today + timedelta(days=1), "明天"

    try:
        parsed = date.fromisoformat(normalized)
        return parsed, f"{parsed:%Y-%m-%d}"
    except ValueError:
        return None, ""


def _strip_command(message_str: str, commands: set[str]) -> str:
    text = str(message_str or "").strip()
    if not text:
        return ""

    if text[0] in {"/", "／"}:
        text = text[1:].lstrip()

    for command in sorted(commands, key=len, reverse=True):
        if text == command:
            return ""
        if text.startswith(f"{command} "):
            return text[len(command) :].strip()

    return text


def _help_text() -> str:
    return "\n".join(
        [
            "课程表插件命令：",
            "/课程表 同步群文件 - 群文件与本地 .ics 双向同步，较新的版本覆盖较旧版本",
            "/课程表 导出 - 将自己的原始 .ics 上传到当前群",
            "/课程表 查看 [昵称或QQ号] - 查看自己或群友的完整课程表",
            "/课程表 列表 - 列出当前会话已保存课程表成员",
            "/课程表 保存 <课程表内容> - 手动保存文本课程表",
            "/课程表 删除 - 删除自己当前会话下的课程表",
            "/查看课表 - 快速查看自己完整课程表",
            "/查看明日课表 - 查看自己明天课程",
            "/群友在上什么课 - 群友当前或下一节课图片",
            "/群友明天上什么课 - 群友明日课程图片",
            "/本周上课排行 - 本周群友上课时长排行图片",
            "",
            "群文件支持两种放法：",
            "1. 根目录 schedule123456.ics",
            "2. schedule/123456.ics",
            "其中 123456 是 QQ 号。",
            "",
            "兼容短命令：",
            "/课程表同步群文件",
            "/课程表导出",
            "/课程表查看",
            "/课程表列表",
            "/课程表保存",
            "/课程表删除",
            "",
            "示例：",
            "/课程表 同步群文件",
            "/群友在上什么课",
        ]
    )
