from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

from .constants import LOCAL_TZ, SCHEDULE_FOLDER_NAME, STORE_KEY
from .files import (
    _display_group_file_path,
    _download_text,
    _extract_schedule_user_id,
    _file_name,
    _folder_name,
    _inline_file_uris,
    _is_normalized_schedule_file,
    _join_folder_path,
    _local_file_uri,
    _normalized_schedule_filename,
    _normalized_schedule_path,
    _write_temp_schedule_ics,
)
from .ics import _format_ics_schedule, _parse_schedule_ics
from .occurrences import (
    _current_or_next,
    _day_bounds,
    _duration_hours,
    _expand_member_occurrences,
    _format_occurrence_line,
    _week_bounds,
)
from .render import _draw_rows_image
from .sql_edit import apply_sql_edit_to_member
from .sql_query import execute_course_schedule_sql
from .store import (
    _compare_timestamps,
    _empty_store,
    _group_file_updated_at,
    _local_schedule_updated_at,
    _normalize_store,
    _scope_id,
    _scope_label,
)
from .texts import _format_time, _is_own_query, _parse_date_query
from .time_utils import _now_iso, _timestamp_iso


class CourseScheduleBase:
    async def _load_store(self) -> dict[str, Any]:
        return _normalize_store(await self.get_kv_data(STORE_KEY, _empty_store()))

    async def _save_store(self, store: dict[str, Any]) -> None:
        await self.put_kv_data(STORE_KEY, store)

    async def _get_scope_members(
        self, event: AstrMessageEvent, create: bool = False
    ) -> dict[str, Any]:
        store = await self._load_store()
        groups = store["groups"]
        scope = _scope_id(event)

        if create:
            scope_data = groups.setdefault(scope, {"members": {}})
        else:
            scope_data = groups.get(scope, {"members": {}})

        members = scope_data.get("members")
        if not isinstance(members, dict):
            members = {}
            scope_data["members"] = members

        if create:
            await self._save_store(store)

        return members

    async def _upsert_ics_schedule(
        self,
        event: AstrMessageEvent,
        user_id: str,
        filename: str,
        ics_content: str,
        uploader_id: str | None = None,
        name: str | None = None,
        remote_updated_at: Any | None = None,
    ) -> None:
        events, schedule_text = _parse_schedule_ics(ics_content)
        now = _now_iso()
        schedule_updated_at = _timestamp_iso(remote_updated_at)
        store = await self._load_store()
        scope = _scope_id(event)
        scope_data = store["groups"].setdefault(scope, {"members": {}})
        members = scope_data.setdefault("members", {})
        previous = members.get(user_id, {})

        members[user_id] = {
            "name": name or previous.get("name") or user_id,
            "schedule": schedule_text,
            "updated_at": now,
            "schedule_updated_at": schedule_updated_at,
            "remote_updated_at": schedule_updated_at,
            "last_synced_at": now,
            "source": "ics",
            "source_file": filename,
            "uploader_id": uploader_id or "",
            "event_count": len(events),
            "events": events,
            "ics": ics_content,
        }
        await self._save_store(store)

    async def _mark_schedule_synced(
        self,
        event: AstrMessageEvent,
        user_id: str,
        source_file: str,
        remote_updated_at: Any | None = None,
    ) -> None:
        store = await self._load_store()
        scope_data = store["groups"].get(_scope_id(event))
        if not isinstance(scope_data, dict):
            return

        members = scope_data.get("members")
        if not isinstance(members, dict):
            return

        info = members.get(user_id)
        if not isinstance(info, dict):
            return

        now = _now_iso()
        synced_at = _timestamp_iso(remote_updated_at)
        info["source_file"] = source_file
        info["schedule_updated_at"] = synced_at
        info["remote_updated_at"] = synced_at
        info["last_synced_at"] = now
        info["updated_at"] = now
        await self._save_store(store)

    async def _resolve_member_info(
        self, event: AstrMessageEvent, query: str = ""
    ) -> tuple[str | None, dict[str, Any] | None, str | None]:
        members = await self._get_scope_members(event)
        if not members:
            return None, None, "当前会话还没有保存任何课程表。"

        normalized_query = str(query or "").strip()
        target_id = event.get_sender_id()

        if not _is_own_query(normalized_query):
            matched_ids = [
                user_id
                for user_id, info in members.items()
                if isinstance(info, dict)
                and (
                    normalized_query == user_id
                    or normalized_query in str(info.get("name", ""))
                )
            ]
            if not matched_ids:
                return None, None, f"没有找到“{normalized_query}”的课程表。"

            if len(matched_ids) > 1:
                names = [
                    f"{members[user_id].get('name') or user_id}({user_id})"
                    for user_id in matched_ids[:10]
                    if isinstance(members.get(user_id), dict)
                ]
                return None, None, "找到多个匹配成员，请用 QQ 号精确查询：\n" + "\n".join(names)

            target_id = matched_ids[0]

        info = members.get(target_id)
        if not isinstance(info, dict):
            if normalized_query and not _is_own_query(normalized_query):
                return None, None, f"没有找到“{normalized_query}”的课程表。"

            return None, None, "你还没有保存课程表，请先上传 .ics 并使用 /课程表 同步群文件。"

        return target_id, info, None

    async def _show_schedule_text(self, event: AstrMessageEvent, query: str = "") -> str:
        target_id, info, error = await self._resolve_member_info(event, query)
        if error:
            return error

        name = info.get("name") or target_id
        schedule = info.get("schedule") or "空"
        updated_at = _format_time(info.get("updated_at"))
        return f"{name} 的课程表：\n{schedule}\n\n更新时间：{updated_at}"

    async def _list_schedules_text(self, event: AstrMessageEvent) -> str:
        members = await self._get_scope_members(event)
        if not members:
            return "当前会话还没有保存任何课程表。"

        lines = [
            f"{info.get('name') or user_id}({user_id})"
            for user_id, info in members.items()
            if isinstance(info, dict)
        ]
        return f"{_scope_label(event)}已保存 {len(lines)} 份课程表：\n" + "\n".join(lines)

    async def _save_manual_schedule_text(
        self, event: AstrMessageEvent, schedule_text: str
    ) -> str:
        content = str(schedule_text or "").strip()
        if not content:
            return "请提供要保存的课程表内容。"

        now = _now_iso()
        user_id = event.get_sender_id()
        store = await self._load_store()
        scope = _scope_id(event)
        scope_data = store["groups"].setdefault(scope, {"members": {}})
        members = scope_data.setdefault("members", {})
        previous = members.get(user_id, {})
        members[user_id] = {
            "name": event.get_sender_name() or previous.get("name") or user_id,
            "schedule": content,
            "updated_at": now,
            "content_updated_at": now,
            "last_synced_at": now,
            "source": "manual",
            "source_file": "",
            "uploader_id": user_id,
            "event_count": 0,
            "events": [],
            "ics": "",
        }
        await self._save_store(store)
        return "已保存文本课程表。"

    async def _delete_own_schedule_text(self, event: AstrMessageEvent) -> str:
        store = await self._load_store()
        scope = _scope_id(event)
        scope_data = store["groups"].get(scope)
        if not isinstance(scope_data, dict):
            return "你还没有保存课程表。"

        members = scope_data.get("members")
        if not isinstance(members, dict):
            return "你还没有保存课程表。"

        removed = members.pop(event.get_sender_id(), None)
        if removed is None:
            return "你还没有保存课程表。"

        await self._save_store(store)
        return "已删除你在当前会话中的课程表。"

    async def _get_local_ics_text(self, event: AstrMessageEvent, query: str = "") -> str:
        target_id, info, error = await self._resolve_member_info(event, query)
        if error:
            return error

        ics_content = str(info.get("ics") or "")
        if not ics_content:
            return "当前课程表不是 .ics 导入的数据，无法读取本地 .ics 内容。"

        name = info.get("name") or target_id
        updated_at = _format_time(info.get("updated_at"))
        source_file = info.get("source_file") or _normalized_schedule_path(target_id)
        return (
            f"{name}({target_id}) 的本地 .ics 内容如下。\n"
            f"来源文件：{source_file}\n"
            f"本地更新时间：{updated_at}\n\n"
            f"{ics_content}"
        )

    async def _replace_local_ics_text(
        self, event: AstrMessageEvent, ics_content: str, query: str = ""
    ) -> str:
        content = str(ics_content or "").strip()
        if not content:
            return "新的 .ics 内容为空，未修改。"

        try:
            events, schedule_text = _parse_schedule_ics(content)
        except Exception as exc:
            return f"解析新的 .ics 内容失败，未修改：{exc}"

        if not events:
            return "新的 .ics 内容中没有可解析的 VEVENT，未修改。"

        store = await self._load_store()
        scope_data = store["groups"].get(_scope_id(event))
        if not isinstance(scope_data, dict):
            return "当前会话还没有保存任何课程表。"

        members = scope_data.get("members")
        if not isinstance(members, dict):
            return "当前会话还没有保存任何课程表。"

        target_id, _info, error = await self._resolve_member_info(event, query)
        if error:
            return error

        info = members.get(target_id)
        if not isinstance(info, dict):
            return "没有找到要修改的本地课程表。"
        if not info.get("ics"):
            return "当前课程表不是 .ics 导入的数据，不能用 .ics 替换。"

        now = _now_iso()
        info["ics"] = content
        info["events"] = events
        info["schedule"] = schedule_text
        info["event_count"] = len(events)
        info["source"] = "ics"
        info["updated_at"] = now
        info["schedule_updated_at"] = now
        info["content_updated_at"] = now
        info["last_modified_by"] = event.get_sender_id()
        info["last_modified_at"] = now
        await self._save_store(store)

        name = info.get("name") or target_id
        return (
            f"已更新 {name}({target_id}) 的本地 .ics 课程表，解析到 {len(events)} 个事件。"
            "远端群文件尚未同步；请使用 /同步课表 按时间戳上传本地较新版本。"
        )

    async def _edit_local_schedule_sql_text(
        self, event: AstrMessageEvent, sql: str, query: str = ""
    ) -> str:
        store = await self._load_store()
        scope_data = store["groups"].get(_scope_id(event))
        if not isinstance(scope_data, dict):
            return "当前会话还没有保存任何课程表。"

        members = scope_data.get("members")
        if not isinstance(members, dict):
            return "当前会话还没有保存任何课程表。"

        target_id, _info, error = await self._resolve_member_info(event, query)
        if error:
            return error

        info = members.get(target_id)
        if not isinstance(info, dict):
            return "没有找到要修改的本地课程表。"
        if not info.get("ics"):
            return "当前课程表不是 .ics 导入的数据，不能用 SQL 修改。"

        try:
            updated_info = apply_sql_edit_to_member(info, sql)
        except ValueError as exc:
            return str(exc)

        changes = updated_info.pop("_sql_edit_changes", 0)
        updated_info["schedule"] = _format_ics_schedule(updated_info["events"])
        updated_info["last_modified_by"] = event.get_sender_id()
        members[target_id] = updated_info
        await self._save_store(store)

        name = updated_info.get("name") or target_id
        return (
            f"已用 SQL 修改 {name}({target_id}) 的本地课程表，影响 {changes} 条，"
            f"当前共有 {updated_info.get('event_count', 0)} 个事件。"
            "远端群文件尚未同步；请使用 /同步课表 按时间戳上传本地较新版本。"
        )

    def _is_onebot_event(self, event: AstrMessageEvent) -> bool:
        return event.get_platform_name() == "aiocqhttp" and hasattr(event, "bot")

    async def _call_onebot_api(self, event: AstrMessageEvent, action: str, **params):
        if not self._is_onebot_event(event):
            raise RuntimeError("当前平台不是 aiocqhttp/OneBot v11，不能调用群文件 API。")

        bot = event.bot
        api = getattr(bot, "api", None)
        if api is None or not hasattr(api, "call_action"):
            raise RuntimeError("当前 OneBot 适配器没有暴露 call_action。")

        return await api.call_action(action, **params)

    async def _get_group_member_name(
        self, event: AstrMessageEvent, group_id: str, user_id: str
    ) -> str:
        try:
            data = self._extract_data(
                await self._call_onebot_api(
                    event,
                    "get_group_member_info",
                    group_id=int(group_id),
                    user_id=int(user_id),
                    no_cache=True,
                )
            )
            if isinstance(data, dict):
                return str(data.get("card") or data.get("nickname") or user_id)
        except Exception:
            pass
        return user_id

    async def _get_group_member_names(
        self, event: AstrMessageEvent, members: dict[str, Any]
    ) -> dict[str, str]:
        group_id = event.get_group_id()
        if not group_id:
            return {
                user_id: str(info.get("name") or user_id)
                for user_id, info in members.items()
                if isinstance(info, dict)
            }

        async def resolve(user_id: str, info: dict[str, Any]) -> tuple[str, str]:
            fetched_name = await self._get_group_member_name(event, group_id, user_id)
            if fetched_name and fetched_name != user_id:
                return user_id, fetched_name
            return user_id, str(info.get("name") or user_id)

        tasks = [
            resolve(user_id, info)
            for user_id, info in members.items()
            if isinstance(info, dict)
        ]
        if not tasks:
            return {}
        return dict(await asyncio.gather(*tasks))

    def _extract_data(self, response: Any) -> Any:
        if isinstance(response, dict) and "data" in response:
            return response["data"]

        return response

    def _extract_file_id(self, file_info: dict[str, Any]) -> str:
        return str(file_info.get("file_id") or file_info.get("id") or "")

    def _extract_folder_id(self, folder_info: dict[str, Any]) -> str:
        return str(folder_info.get("folder_id") or folder_info.get("id") or "")

    def _extract_busid(self, file_info: dict[str, Any]) -> int:
        return int(file_info.get("busid") or file_info.get("bus_id") or 0)

    def _sanitize_upload_error(self, exc: Exception) -> str:
        message = str(exc)
        message = re.sub(r"base64://[A-Za-z0-9+/=_-]+", "base64://<redacted>", message)
        message = re.sub(
            r"data:[^,\s]+;base64,[A-Za-z0-9+/=_-]+",
            "data:<redacted>;base64,<redacted>",
            message,
        )
        return message

    async def _list_group_files_and_folders(
        self, event: AstrMessageEvent, group_id: str
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        root = self._extract_data(
            await self._call_onebot_api(
                event, "get_group_root_files", group_id=int(group_id)
            )
        )
        files: list[dict[str, Any]] = []
        all_folders: list[dict[str, Any]] = []
        for file_info in root.get("files") or []:
            copied = dict(file_info)
            copied["_folder_path"] = ""
            files.append(copied)

        folders: list[tuple[dict[str, Any], str]] = []
        for folder in root.get("folders") or []:
            folder_path = _folder_name(folder)
            copied = dict(folder)
            copied["_folder_path"] = folder_path
            copied["_parent_folder_id"] = ""
            all_folders.append(copied)
            folders.append((copied, folder_path))
        seen_folder_ids: set[str] = set()

        while folders:
            folder, folder_path = folders.pop(0)
            folder_id = self._extract_folder_id(folder)
            if not folder_id or folder_id in seen_folder_ids:
                continue
            seen_folder_ids.add(folder_id)

            child = self._extract_data(
                await self._call_onebot_api(
                    event,
                    "get_group_files_by_folder",
                    group_id=int(group_id),
                    folder_id=folder_id,
                )
            )
            for file_info in child.get("files") or []:
                copied = dict(file_info)
                copied["_folder_path"] = folder_path
                copied["_folder_id"] = folder_id
                files.append(copied)

            for child_folder in child.get("folders") or []:
                child_name = _folder_name(child_folder)
                child_path = _join_folder_path(folder_path, child_name)
                copied = dict(child_folder)
                copied["_folder_path"] = child_path
                copied["_parent_folder_id"] = folder_id
                all_folders.append(copied)
                folders.append((copied, child_path))

        return files, all_folders

    async def _list_group_files(
        self, event: AstrMessageEvent, group_id: str
    ) -> list[dict[str, Any]]:
        files, _folders = await self._list_group_files_and_folders(event, group_id)
        return files

    async def _ensure_schedule_folder(
        self,
        event: AstrMessageEvent,
        group_id: str,
        folders: list[dict[str, Any]] | None = None,
    ) -> str:
        if folders is None:
            _files, folders = await self._list_group_files_and_folders(event, group_id)

        for folder_info in folders:
            folder_path = str(folder_info.get("_folder_path") or "").strip("/").lower()
            if folder_path == SCHEDULE_FOLDER_NAME:
                folder_id = self._extract_folder_id(folder_info)
                if folder_id:
                    return folder_id

        try:
            response = self._extract_data(
                await self._call_onebot_api(
                    event,
                    "create_group_file_folder",
                    group_id=int(group_id),
                    name=SCHEDULE_FOLDER_NAME,
                    parent_id="/",
                )
            )
        except Exception as exc:
            raise RuntimeError(f"创建群文件 schedule 文件夹失败：{exc}") from exc

        if isinstance(response, dict):
            folder_id = str(
                response.get("folder_id")
                or response.get("id")
                or response.get("folder")
                or ""
            )
            if folder_id:
                return folder_id

        _files, folders = await self._list_group_files_and_folders(event, group_id)
        for folder_info in folders:
            folder_path = str(folder_info.get("_folder_path") or "").strip("/").lower()
            if folder_path == SCHEDULE_FOLDER_NAME:
                folder_id = self._extract_folder_id(folder_info)
                if folder_id:
                    return folder_id

        raise RuntimeError("创建群文件 schedule 文件夹后未找到 folder_id")

    async def _delete_group_file(
        self, event: AstrMessageEvent, group_id: str, file_info: dict[str, Any]
    ) -> None:
        file_id = self._extract_file_id(file_info)
        if not file_id:
            raise ValueError("群文件缺少 file_id/id")

        await self._call_onebot_api(
            event,
            "delete_group_file",
            group_id=int(group_id),
            file_id=file_id,
            busid=self._extract_busid(file_info),
        )

    async def _delete_old_schedule_files(
        self,
        event: AstrMessageEvent,
        group_id: str,
        user_id: str,
    ) -> tuple[list[str], list[str]]:
        try:
            files = await self._list_group_files(event, group_id)
        except Exception as exc:
            logger.error(f"Failed to list group files when deleting old files: {exc}")
            return [], [f"{user_id}: 读取群文件以删除旧文件失败：{exc}"]

        matched: list[dict[str, Any]] = []
        for file_info in files:
            matched_user_id = _extract_schedule_user_id(
                _file_name(file_info), str(file_info.get("_folder_path") or "")
            )
            if matched_user_id == user_id:
                matched.append(file_info)

        if not matched:
            return [], []

        normalized_files = [
            file_info
            for file_info in matched
            if _is_normalized_schedule_file(file_info, user_id)
        ]
        keep_file: dict[str, Any] | None = None
        if not normalized_files:
            logger.warning(
                f"Uploaded normalized schedule file for {user_id}, "
                "but it was not found when cleaning old files."
            )
            return [], [
                f"{user_id}: 上传后未找到 {_normalized_schedule_path(user_id)}，"
                "已跳过删除旧文件"
            ]

        for file_info in normalized_files:
            if keep_file is None:
                keep_file = file_info
                continue

            file_time = _group_file_updated_at(file_info)
            keep_time = _group_file_updated_at(keep_file)
            if _compare_timestamps(file_time, keep_time) == 1 or (
                file_time and not keep_time
            ):
                keep_file = file_info

        keep_time = _group_file_updated_at(keep_file) if keep_file else None
        deleted: list[str] = []
        failed: list[str] = []
        for file_info in matched:
            if file_info is keep_file:
                continue
            if _is_normalized_schedule_file(file_info, user_id):
                file_time = _group_file_updated_at(file_info)
                if not file_time or not keep_time:
                    continue

            display_path = _display_group_file_path(file_info)
            try:
                await self._delete_group_file(event, group_id, file_info)
                deleted.append(display_path)
            except Exception as exc:
                logger.error(
                    f"Failed to delete old group schedule file {display_path}: {exc}"
                )
                failed.append(f"{display_path}: {exc}")

        return deleted, failed

    async def _get_group_file_url(
        self, event: AstrMessageEvent, group_id: str, file_info: dict[str, Any]
    ) -> str:
        file_id = self._extract_file_id(file_info)
        busid = self._extract_busid(file_info)
        if not file_id:
            raise ValueError("群文件缺少 file_id/id")

        response = self._extract_data(
            await self._call_onebot_api(
                event,
                "get_group_file_url",
                group_id=int(group_id),
                file_id=file_id,
                busid=busid,
            )
        )
        if isinstance(response, dict):
            url = response.get("url")
        else:
            url = None

        if not url:
            raise ValueError("协议端没有返回群文件下载链接")

        return str(url)

    async def _upload_schedule_file(
        self,
        event: AstrMessageEvent,
        group_id: str,
        user_id: str,
        ics_content: str,
        schedule_folder_id: str | None = None,
    ) -> str:
        default_filename, local_path = await asyncio.to_thread(
            _write_temp_schedule_ics, user_id, ics_content
        )
        filename = _normalized_schedule_filename(user_id) or default_filename
        folder_id = str(schedule_folder_id or "")
        if not folder_id:
            folder_id = await self._ensure_schedule_folder(event, group_id)

        params: dict[str, Any] = {
            "group_id": int(group_id),
            "file": local_path,
            "name": filename,
            "folder": folder_id,
        }

        upload_attempts: list[dict[str, Any]] = []
        upload_files = [
            local_path,
            _local_file_uri(local_path),
            *_inline_file_uris(ics_content),
        ]
        for upload_file in upload_files:
            attempt = dict(params)
            attempt["file"] = upload_file
            upload_attempts.append(attempt)

        last_exc: Exception | None = None
        for attempt in upload_attempts:
            try:
                await self._call_onebot_api(event, "upload_group_file", **attempt)
                break
            except Exception as exc:
                last_exc = exc
        else:
            if last_exc:
                raise RuntimeError(
                    "upload_group_file failed after trying local path, file URI, "
                    "base64 URI and data URI: "
                    f"{self._sanitize_upload_error(last_exc)}"
                ) from last_exc

        return _normalized_schedule_path(user_id)

    async def _download_group_schedule(
        self,
        event: AstrMessageEvent,
        group_id: str,
        user_id: str,
        file_info: dict[str, Any],
        remote_updated_at: datetime | None,
    ) -> str:
        url = await self._get_group_file_url(event, group_id, file_info)
        ics_content = await asyncio.to_thread(_download_text, url)
        member_name = await self._get_group_member_name(event, group_id, user_id)
        await self._upsert_ics_schedule(
            event,
            user_id=user_id,
            filename=_display_group_file_path(file_info),
            ics_content=ics_content,
            uploader_id=str(file_info.get("uploader") or file_info.get("user_id") or ""),
            name=member_name,
            remote_updated_at=remote_updated_at,
        )
        return ics_content

    async def _sync_group_files_text(self, event: AstrMessageEvent) -> str:
        group_id = event.get_group_id()
        if not group_id:
            return "只能在群聊中同步群文件。"

        try:
            files, folders = await self._list_group_files_and_folders(event, group_id)
        except Exception as exc:
            logger.error(f"Failed to list group files: {exc}")
            return f"读取群文件失败：{exc}"

        matched = []
        for file_info in files:
            filename = _file_name(file_info)
            folder_path = str(file_info.get("_folder_path") or "")
            if _extract_schedule_user_id(filename, folder_path):
                matched.append(file_info)

        members = await self._get_scope_members(event)
        remote_by_user: dict[str, dict[str, Any]] = {}
        for file_info in matched:
            user_id = _extract_schedule_user_id(
                _file_name(file_info), str(file_info.get("_folder_path") or "")
            )
            if not user_id:
                continue

            current = remote_by_user.get(user_id)
            if not current:
                remote_by_user[user_id] = file_info
                continue

            current_time = _group_file_updated_at(current)
            next_time = _group_file_updated_at(file_info)
            comparison = _compare_timestamps(next_time, current_time)
            if comparison == 1 or (next_time and not current_time):
                remote_by_user[user_id] = file_info
            elif (
                comparison == 0
                and _is_normalized_schedule_file(file_info, user_id)
                and not _is_normalized_schedule_file(current, user_id)
            ):
                remote_by_user[user_id] = file_info

        downloaded: list[str] = []
        uploaded: list[str] = []
        skipped: list[str] = []
        deleted_old: list[str] = []
        failed: list[str] = []
        processed_users: set[str] = set()
        schedule_folder_id: str | None = None

        for user_id, file_info in remote_by_user.items():
            display_path = _display_group_file_path(file_info)
            processed_users.add(user_id)
            remote_updated_at = _group_file_updated_at(file_info)
            local_info = members.get(user_id)
            local_updated_at = (
                _local_schedule_updated_at(local_info)
                if isinstance(local_info, dict)
                else None
            )
            comparison = _compare_timestamps(local_updated_at, remote_updated_at)

            try:
                if (
                    comparison == 1
                    and isinstance(local_info, dict)
                    and local_info.get("ics")
                ):
                    if schedule_folder_id is None:
                        schedule_folder_id = await self._ensure_schedule_folder(
                            event, group_id, folders
                        )
                    uploaded_name = await self._upload_schedule_file(
                        event,
                        group_id,
                        user_id,
                        str(local_info["ics"]),
                        schedule_folder_id=schedule_folder_id,
                    )
                    await self._mark_schedule_synced(
                        event, user_id, uploaded_name, datetime.now(timezone.utc)
                    )
                    uploaded.append(f"{user_id} -> {uploaded_name}")
                    deleted, delete_failed = await self._delete_old_schedule_files(
                        event, group_id, user_id
                    )
                    deleted_old.extend(deleted)
                    failed.extend(delete_failed)
                elif comparison == 0 and isinstance(local_info, dict):
                    if _is_normalized_schedule_file(file_info, user_id):
                        await self._mark_schedule_synced(
                            event, user_id, display_path, remote_updated_at
                        )
                        skipped.append(f"{display_path} 已是最新")
                        deleted, delete_failed = await self._delete_old_schedule_files(
                            event, group_id, user_id
                        )
                        deleted_old.extend(deleted)
                        failed.extend(delete_failed)
                    else:
                        ics_content = str(local_info.get("ics") or "")
                        if not ics_content:
                            ics_content = await self._download_group_schedule(
                                event, group_id, user_id, file_info, remote_updated_at
                            )
                            downloaded.append(f"{display_path} -> {user_id}")

                        if schedule_folder_id is None:
                            schedule_folder_id = await self._ensure_schedule_folder(
                                event, group_id, folders
                            )
                        uploaded_name = await self._upload_schedule_file(
                            event,
                            group_id,
                            user_id,
                            ics_content,
                            schedule_folder_id=schedule_folder_id,
                        )
                        await self._mark_schedule_synced(
                            event, user_id, uploaded_name, datetime.now(timezone.utc)
                        )
                        uploaded.append(f"{user_id} -> {uploaded_name}")
                        deleted, delete_failed = await self._delete_old_schedule_files(
                            event, group_id, user_id
                        )
                        deleted_old.extend(deleted)
                        failed.extend(delete_failed)
                else:
                    ics_content = await self._download_group_schedule(
                        event, group_id, user_id, file_info, remote_updated_at
                    )
                    downloaded.append(f"{display_path} -> {user_id}")
                    if not _is_normalized_schedule_file(file_info, user_id):
                        if schedule_folder_id is None:
                            schedule_folder_id = await self._ensure_schedule_folder(
                                event, group_id, folders
                            )
                        uploaded_name = await self._upload_schedule_file(
                            event,
                            group_id,
                            user_id,
                            ics_content,
                            schedule_folder_id=schedule_folder_id,
                        )
                        await self._mark_schedule_synced(
                            event, user_id, uploaded_name, datetime.now(timezone.utc)
                        )
                        uploaded.append(f"{user_id} -> {uploaded_name}")
                        deleted, delete_failed = await self._delete_old_schedule_files(
                            event, group_id, user_id
                        )
                        deleted_old.extend(deleted)
                        failed.extend(delete_failed)
                    else:
                        deleted, delete_failed = await self._delete_old_schedule_files(
                            event, group_id, user_id
                        )
                        deleted_old.extend(deleted)
                        failed.extend(delete_failed)
            except Exception as exc:
                logger.error(f"Failed to sync group file {display_path}: {exc}")
                failed.append(f"{display_path}: {exc}")

        for user_id, local_info in members.items():
            if user_id in processed_users or not isinstance(local_info, dict):
                continue
            ics_content = local_info.get("ics")
            if not ics_content:
                continue

            try:
                if schedule_folder_id is None:
                    schedule_folder_id = await self._ensure_schedule_folder(
                        event, group_id, folders
                    )
                uploaded_name = await self._upload_schedule_file(
                    event,
                    group_id,
                    user_id,
                    str(ics_content),
                    schedule_folder_id=schedule_folder_id,
                )
                await self._mark_schedule_synced(
                    event, user_id, uploaded_name, datetime.now(timezone.utc)
                )
                uploaded.append(f"{user_id} -> {uploaded_name}")
                deleted, delete_failed = await self._delete_old_schedule_files(
                    event, group_id, user_id
                )
                deleted_old.extend(deleted)
                failed.extend(delete_failed)
            except Exception as exc:
                logger.error(f"Failed to upload local schedule for {user_id}: {exc}")
                failed.append(f"{user_id}: {exc}")

        lines = [
            "群文件双向同步完成："
            f"下载 {len(downloaded)} 个，上传 {len(uploaded)} 个，"
            f"删除旧文件 {len(deleted_old)} 个，跳过 {len(skipped)} 个，"
            f"失败 {len(failed)} 个。"
        ]
        if downloaded:
            lines.append("已下载：")
            lines.extend(downloaded[:20])
        if uploaded:
            lines.append("已上传：")
            lines.extend(uploaded[:20])
        if deleted_old:
            lines.append("已删除旧文件：")
            lines.extend(deleted_old[:20])
        if skipped:
            lines.append("已跳过：")
            lines.extend(skipped[:10])
        if failed:
            lines.append("失败：")
            lines.extend(failed[:10])

        return "\n".join(lines)

    async def _export_own_ics_text(self, event: AstrMessageEvent) -> str:
        group_id = event.get_group_id()
        if not group_id:
            return "只能在群聊中上传课程表文件。"

        target_id, info, error = await self._resolve_member_info(event)
        if error:
            return "你还没有保存课程表。请先上传 schedule/<QQ号>.ics 并同步。"

        ics_content = info.get("ics")
        if not ics_content:
            return "当前课程表不是从 .ics 导入的，无法导出原始 .ics。"

        user_id = target_id or event.get_sender_id()
        filename = _normalized_schedule_path(user_id)
        try:
            schedule_folder_id = await self._ensure_schedule_folder(event, group_id)
            filename = await self._upload_schedule_file(
                event,
                group_id,
                user_id,
                str(ics_content),
                schedule_folder_id=schedule_folder_id,
            )
            await self._mark_schedule_synced(
                event,
                user_id,
                filename,
                datetime.now(timezone.utc),
            )
            deleted, delete_failed = await self._delete_old_schedule_files(
                event, group_id, user_id
            )
        except Exception as exc:
            logger.error(f"Failed to upload group file {filename}: {exc}")
            return f"上传群文件失败：{exc}"

        message = f"已上传 {filename} 到当前群文件。"
        if deleted:
            message += f"\n已删除旧文件 {len(deleted)} 个。"
        if delete_failed:
            message += "\n旧文件删除失败：\n" + "\n".join(delete_failed[:5])
        return message

    async def _member_day_schedule_text(
        self, event: AstrMessageEvent, query: str, day: str
    ) -> str:
        target_date, title = _parse_date_query(day)
        if not target_date:
            return "无法识别日期，请使用 today、tomorrow、今天、明天或 YYYY-MM-DD。"

        target_id, info, error = await self._resolve_member_info(event, query)
        if error:
            return error

        start_bound, end_bound = _day_bounds(target_date)
        occurrences = _expand_member_occurrences(info, start_bound, end_bound)

        name = info.get("name") or target_id
        if occurrences:
            lines = [f"{name} {title}课程："]
            lines.extend(_format_occurrence_line(item) for item in occurrences)
            return "\n".join(lines)

        if info.get("events"):
            return f"{name} {title}没有接下来要上的课程。"

        schedule = info.get("schedule") or "空"
        return (
            f"{name} 的课程表不是结构化 .ics 数据，无法按日期展开。"
            f"\n已保存内容：\n{schedule}"
        )

    async def _group_current_text(self, event: AstrMessageEvent) -> str:
        members = await self._get_scope_members(event)
        if not members:
            return "当前会话还没有保存任何课程表。"

        now = datetime.now(LOCAL_TZ)
        start_bound, end_bound = _day_bounds(now.date())
        names = await self._get_group_member_names(event, members)
        rows: list[tuple[str, str, str, str]] = []
        for user_id, info in members.items():
            if not isinstance(info, dict):
                continue

            occurrences = _expand_member_occurrences(info, start_bound, end_bound)
            status, occurrence = _current_or_next(occurrences, now)
            if not occurrence:
                continue

            course = occurrence.get("SUMMARY") or "未命名课程"
            location = occurrence.get("LOCATION")
            if location:
                course += f" @ {location}"
            time_text = f"{occurrence['_start']:%H:%M}-{occurrence['_end']:%H:%M}"
            name = names.get(user_id) or str(info.get("name") or user_id)
            rows.append((status, time_text, name, course))

        if not rows:
            return "当前会话没有可展示的当前或下一节课程。"

        rows.sort(key=lambda row: (row[0] != "正在上", row[1], row[2]))
        lines = ["群友当前 / 下一节课："]
        lines.extend(
            f"{name}：{status} {time_text} {course}"
            for status, time_text, name, course in rows
        )
        return "\n".join(lines)

    async def _query_schedule_sql_text(
        self, event: AstrMessageEvent, sql: str, time_range: str = "today"
    ) -> str:
        members = await self._get_scope_members(event)
        if not members:
            return "当前会话还没有保存任何课程表。"

        names = await self._get_group_member_names(event, members)
        return await asyncio.to_thread(
            execute_course_schedule_sql,
            members,
            names,
            sql,
            time_range,
            datetime.now(LOCAL_TZ),
        )

    async def _group_current_image(self, event: AstrMessageEvent) -> str | None:
        members = await self._get_scope_members(event)
        if not members:
            return None

        now = datetime.now(LOCAL_TZ)
        start_bound, end_bound = _day_bounds(now.date())
        rows: list[dict[str, str]] = []
        names = await self._get_group_member_names(event, members)
        for user_id, info in members.items():
            if not isinstance(info, dict):
                continue
            occurrences = _expand_member_occurrences(info, start_bound, end_bound)
            status, occurrence = _current_or_next(occurrences, now)
            if not occurrence:
                continue

            course = occurrence.get("SUMMARY") or "未命名课程"
            location = occurrence.get("LOCATION")
            if location:
                course += f" @ {location}"
            rows.append(
                {
                    "user_id": user_id,
                    "name": names.get(user_id) or str(info.get("name") or user_id),
                    "subtitle": user_id,
                    "status": status,
                    "course": course,
                    "time": f"{occurrence['_start']:%H:%M}-{occurrence['_end']:%H:%M}",
                }
            )

        rows.sort(key=lambda row: (row["status"] != "正在上", row["time"], row["name"]))
        return _draw_rows_image("群友当前 / 下一节课", rows, "group_current.png")

    async def _group_today_image(self, event: AstrMessageEvent) -> str | None:
        members = await self._get_scope_members(event)
        if not members:
            return None

        now = datetime.now(LOCAL_TZ)
        start_bound, end_bound = _day_bounds(now.date())
        rows: list[dict[str, str]] = []
        names = await self._get_group_member_names(event, members)
        for user_id, info in members.items():
            if not isinstance(info, dict):
                continue
            occurrences = _expand_member_occurrences(info, start_bound, end_bound)
            for occurrence in occurrences:
                course = occurrence.get("SUMMARY") or "未命名课程"
                location = occurrence.get("LOCATION")
                if location:
                    course += f" @ {location}"

                status = "今天"
                if occurrence["_start"] <= now < occurrence["_end"]:
                    status = "正在上"
                elif occurrence["_end"] <= now:
                    status = "已结束"

                rows.append(
                    {
                        "user_id": user_id,
                        "name": names.get(user_id) or str(info.get("name") or user_id),
                        "subtitle": user_id,
                        "status": status,
                        "course": course,
                        "time": f"{occurrence['_start']:%H:%M}-{occurrence['_end']:%H:%M}",
                    }
                )

        rows.sort(key=lambda row: (row["time"], row["name"], row["course"]))
        return _draw_rows_image(f"今日课程表 {now:%Y-%m-%d}", rows, "group_today.png")

    async def _group_tomorrow_image(self, event: AstrMessageEvent) -> str | None:
        members = await self._get_scope_members(event)
        if not members:
            return None

        target_date = datetime.now(LOCAL_TZ).date() + timedelta(days=1)
        start_bound, end_bound = _day_bounds(target_date)
        rows: list[dict[str, str]] = []
        names = await self._get_group_member_names(event, members)
        for user_id, info in members.items():
            if not isinstance(info, dict):
                continue
            occurrences = _expand_member_occurrences(info, start_bound, end_bound)
            if not occurrences:
                continue

            first = occurrences[0]
            course = first.get("SUMMARY") or "未命名课程"
            location = first.get("LOCATION")
            if location:
                course += f" @ {location}"
            rows.append(
                {
                    "user_id": user_id,
                    "name": names.get(user_id) or str(info.get("name") or user_id),
                    "subtitle": f"共 {len(occurrences)} 节",
                    "status": "明天",
                    "course": course,
                    "time": f"{first['_start']:%H:%M}-{first['_end']:%H:%M}",
                }
            )

        rows.sort(key=lambda row: (row["time"], row["name"]))
        return _draw_rows_image("群友明日课程", rows, "group_tomorrow.png")

    async def _weekly_rank_image(self, event: AstrMessageEvent) -> str | None:
        members = await self._get_scope_members(event)
        if not members:
            return None

        today = datetime.now(LOCAL_TZ).date()
        start_bound, end_bound = _week_bounds(today)
        rows: list[dict[str, str]] = []
        names = await self._get_group_member_names(event, members)
        for user_id, info in members.items():
            if not isinstance(info, dict):
                continue
            occurrences = _expand_member_occurrences(info, start_bound, end_bound)
            if not occurrences:
                continue

            hours = _duration_hours(occurrences)
            rows.append(
                {
                    "user_id": user_id,
                    "name": names.get(user_id) or str(info.get("name") or user_id),
                    "subtitle": user_id,
                    "status": f"{len(occurrences)} 节",
                    "course": f"{hours:.1f} 小时",
                    "time": f"{start_bound:%m/%d}-{(end_bound - timedelta(days=1)):%m/%d}",
                    "_hours": f"{hours:010.3f}",
                }
            )

        rows.sort(key=lambda row: (row["_hours"], row["status"]), reverse=True)
        for index, row in enumerate(rows, start=1):
            row["subtitle"] = f"第 {index} 名 · {row['subtitle']}"

        return _draw_rows_image("本周上课排行", rows, "weekly_rank.png")
