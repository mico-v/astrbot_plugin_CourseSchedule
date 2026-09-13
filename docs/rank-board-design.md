# 群友上课时长榜 —— 功能研究与开发方案

## 1. 背景与结论

### 1.1 这个功能以前存在过

`/本周上课排行` 在 v0.6.0（commit `50ac2ab`）中实现过，在 `7778051`（refact: real sql and ics regx）重写存储层时被整体删除：

```python
# 50ac2ab:plugin/course_schedule.py:1210
async def _weekly_rank_image(self, event) -> str | None:
    ...
    hours = _duration_hours(occurrences)
    rows.append({
        "user_id": user_id,
        "name": names.get(user_id) or str(info.get("name") or user_id),
        "subtitle": user_id,
        "status": f"{len(occurrences)} 节",
        "course": f"{hours:.1f} 小时",
        "time": f"{start_bound:%m/%d}-...",
        "_hours": f"{hours:010.3f}",
    })
    rows.sort(key=lambda row: (row["_hours"], row["status"]), reverse=True)
    for index, row in enumerate(rows, start=1):
        row["subtitle"] = f"第 {index} 名 · {row['subtitle']}"
    return _draw_rows_image("本周上课排行", rows, "weekly_rank.png")
```

老实现的问题，也是这次要修的：

| 问题 | 说明 |
| --- | --- |
| 写死"本周" | 无法看今日/本月/自定义区间 |
| 时长 = 所有事件时长直接相加 | 时间冲突的课（同一时段两门）会被重复计一次，榜单虚高 |
| 不裁剪到统计窗口 | 跨天的课（23:00–01:00）在"今日"榜里算满 2 小时 |
| 全天事件按 24 小时计 | `_event_datetimes` 对 DATE-only 事件回退成 `timedelta(days=1)`，一个"放假"标记就能把人送上榜首 |
| 依赖 `_get_group_member_names` | 该 OneBot 群成员 API 已随重构删除，现在昵称只能取自 `schedule_members` 内保存的 `name` |

### 1.2 现在的技术底座已经够用

本次功能是**纯读**能力，不新增表、不改 schema、不需要数据迁移：

- `SQLiteScheduleStore.get_scope_members(scope_id)` 一次拿到该会话全部成员及其 `events`；
- `_expand_member_occurrences(info, start, end)` 已能展开 RRULE / RDATE / EXDATE，且展开有上界（`rrulestr(...).between(a, b)` 不会因无限重复而炸）；
- `_parse_sql_time_range(value, today)` 已支持 今天/明天/昨天/本周/下周/本月/下月/`YYYY-MM-DD`/`YYYY-MM-DD..YYYY-MM-DD`；
- `_duration_hours` 已存在，`merge_intervals`（`plugin/domain.py:289`）已存在且当前无人使用 —— 正好是时长去重叠要用的；
- `_format_duration_minutes`（`plugin/domain.py:101`）已能输出"1小时30分钟"；
- `_draw_rows_image` 的卡片版式可以直接承载榜单。

所以本方案的定位是：**在老功能的基础上，把统计口径做对，把周期参数化，把出口从"一条命令"扩到"命令 / AI 工具 / Web"三条。**

---

## 2. 关键设计决策

> **状态：以下 5 条已确认，P1 已实现（见 §7）。** 三条待定项的选择是：统计全窗口（含未上的课）并在卡片上显示"已上 X / 共 Y"、不新增 AI 工具、时长口径用 `union`。

以下 5 条是本方案的核心，每条都给了默认选择。

### 决策 1：什么叫"上课时长"

同一时段撞课必须只算一次，且必须裁剪到统计窗口。

- **主口径 `union`（默认）**：把成员在窗口内的所有课程区段做并集（`merge_intervals`），求和得分钟数。含义是"这个人在这个窗口里被课程占用了多少时间"，物理意义明确，撞课不虚高。
- **备选口径 `sum`**：所有区段直接相加。含义是"这个人的课表上一共排了多少课时"，撞课时会大于实际时间。
- 两者都会**裁剪到窗口**：`start = max(start, start_bound)`、`end = min(end, end_bound)`，再参与计算。

默认 `union`，通过插件配置可切到 `sum`。

### 决策 2：全天事件（DATE-only）不计入时长

`_event_datetimes` 对 `DTSTART;VALUE=DATE:20261001` 这类事件回退成 1 天时长。放假日、考试周标记一旦进榜，单个事件就是 24 小时，榜单直接失去意义。

处理：**默认排除 DATE-only 事件**（`len(DTSTART) == 8`）计入时长与节数，但在行内单独附注"另有 N 个全天日程"。可通过配置打开。

### 决策 3：统计窗口内的"未上的课"算，并单独显示已上部分

周三打开"本周榜"，周四、周五的课**算**（已确认）：

- 时长按**全窗口**统计（`all`），周一是 8 小时，周三看还是 8 小时。榜单稳定，适合当"这周谁最惨"的预期管理，也避免同一张榜一天内数字来回变。
- 同时在卡片上给出"**已上 4小时 / 共 12小时30分钟**"，一个出口覆盖进度诉求。数据上体现为 `elapsed_minutes`（只累计 `end <= now` 的部分），与 `minutes` 并存。
- 若以后想要纯进度榜，`build_rank_rows` 已经支持 `mode` 语义的扩展点（把 `minutes` 换成 `elapsed_minutes` 排序即可），不需要改统计内核。

### 决策 4：不新增 AI 工具

**已确认：不加。** README 明确写了"只向 Agent 暴露两个聚合工具（find / edit）"，保持这一约束。

代价要说清楚：模型问"这周谁上课最多"时只能拉 `find` 的明细自己心算，既不准又费 token。这是有意接受的取舍。如果以后改主意，`build_rank_rows` 的输出加一个 `format_rank_text` 就能直接接上 `rank` 工具，不需要动统计层。

### 决策 5：榜单数量的上限

- 图片只展示 **Top N（默认 20）**，尾部用 footer 说明"还有 N 位成员未展示"。原因：`_fetch_avatar` 是**同步阻塞**的 `urlopen`（超时 5s，`lru_cache` 只能跨调用复用），50 人就是 50 次串行网络请求，会把 AstrBot 事件循环卡住。
- 渲染与聚合都走 `await asyncio.to_thread(...)`，不阻塞事件循环。

---

## 3. 统计口径的准确定义

对会话 `scope_id`、窗口 `[start_bound, end_bound)`、当前时刻 `now`：

1. 取 `members = store.get_scope_members(scope_id)`；
2. 对每个成员 `occurrences = _expand_member_occurrences(info, start_bound, end_bound)`；
3. 过滤：默认剔除 DATE-only 事件（决策 2）、剔除命中 `rank_exclude_keywords` 的课程名；
4. 裁剪：`(max(_start, start_bound), min(_end, end_bound))`，丢弃 `end <= start` 的零长度区间；
5. `minutes`：
   - `union` → `sum(e - s for s, e in merge_intervals(intervals))`
   - `sum`  → `sum(e - s for s, e in intervals)`
6. `course_count` = 参与计算的 occurrence 条数（节数）；`course_names` = 去重后的 `SUMMARY` 数（门数）；
7. `elapsed_minutes` = 只对 `_end <= now` 的区间做同样计算，用于"已上/未上"；
8. 排序：`(-minutes, -course_count, name.casefold(), user_id)`；
9. 名次：并列者同名次（competition ranking，1/2/2/4），下一名跳过空位；
10. 无课表 / 无事件的成员默认不进榜；`include_empty=True` 时排在末尾并标注"未登记课表"。

**注意**：RRULE 无 `UNTIL` 的课在无限期重复。导入一个学期的周课表后，"下个月""明年三月"的榜单仍会把它算进去。建议在最终图片 footer 标注"重复课程按 RRULE 展开"，并给窗口加一个 `rank_max_range_days`（默认 366）上限，超出直接返回提示而不是硬算。

---

## 4. 落地方案

### 4.1 新增 `plugin/rank.py`（纯函数，可独立测试）

已实现：

```python
DEFAULT_RANK_PERIOD = "本周"
DEFAULT_RANK_TOP_N = 20
DEFAULT_RANK_METRIC = "union"          # union | sum
RANK_MAX_RANGE_DAYS = 366

def clipped_occurrences(occurrences, start_bound, end_bound, *,
                        include_all_day: bool = False,
                        exclude_keywords: Sequence[str] = ()) -> list[dict]:
    """把 occurrence 裁剪到窗口并过滤；这是所有时长计算的前置步骤。"""

def build_rank_rows(members, start_bound, end_bound, *, now=None,
                    metric: str = DEFAULT_RANK_METRIC,
                    include_empty: bool = False,
                    include_all_day: bool = False,
                    exclude_keywords: Sequence[str] = ()) -> list[dict]:
    """返回按名次排序的行，字段：
       rank, user_id, name, minutes, hours_text, elapsed_minutes, elapsed_text,
       course_count, course_names, all_day_count, progress
    """
```

`rank` 为 0 表示该成员在窗口内没有可计入的课程（只在 `include_empty=True` 时出现）；并列时长共享同一名次。区间文案直接复用 `_parse_sql_time_range` 已返回的 label，不另写一份。

`rank.py` 只依赖 `constants` / `occurrences` / `domain`，不 import astrbot —— 与 `tests/test_daily_schedule.py` 的独立加载方式一致，测试不需要 stub AstrBot。

### 4.2 `plugin/sql_query.py`：补齐周期词

`_parse_sql_time_range` 已有 下周 / 下月，缺 上周 / 上月。补上（同时让 `find` 的 `time_range` 一起受益）：

```python
elif compact in {"lastweek", "上周", "上一周"}:
    start_date = today - timedelta(days=today.weekday()) - timedelta(days=7)
    end_date = start_date + timedelta(days=6)
elif compact in {"lastmonth", "上月", "上个月"}:
    start_date, end_date = _month_bounds(today, -1)
```

### 4.3 `plugin/render.py`：把 `_draw_rows_image` 参数化

不动版式，只把 4 处写死的文案/配色提到参数上，默认值保持现有行为（对今日/明日课表零影响）：

已实现的 4 个新参数（默认值保持原有行为，对今日/明日课表零影响）：

```python
def _draw_rows_image(
    title: str, rows: list[dict[str, object]], filename: str, *,
    subtitle: str | None = None,                    # 默认 "共 N 位成员 · X 人正在上课 · Y 人待上课"
    legend: list[tuple[str, str]] | None = None,    # 默认 正在上课/下一节即将上/今日已结束
    duration_label: str = "本节持续",                # 榜单传 ""，整行由 duration 自带
    footer: str = "实时状态 · 课程时间以本地时区为准",
) -> str
```

`_status_colors` 增加榜单名次色（与现有 `(foreground, background, accent)` 三元组同构，复用同一套卡片渲染）：

```python
"rank1": ("#b45309", "#fef3c7", "#f59e0b"),
"rank2": ("#475569", "#e2e8f0", "#94a3b8"),
"rank3": ("#9a3412", "#ffedd5", "#fb923c"),
"rank":  ("#1d4ed8", "#dbeafe", "#60a5fa"),
```

榜单行的字段映射（不改渲染代码，只改喂进去的数据）：

| 卡片位置 | 今日课表 | 时长榜 |
| --- | --- | --- |
| 头像左侧 | 头像 | 头像（保留） |
| 大标题 | 课程名 | `12小时30分钟` |
| 第二行 | `09:00 - 10:30 · A101` | `8 节 · 5 门课` |
| 第三行 | `本节持续 1小时30分钟` | `已上 4小时 / 12小时30分钟` |
| 进度条 | 本节课进度 | `minutes / 榜首 minutes` 相对占比 |
| 右上角徽章 | 正在上课 | `#1` |
| 右下角 | `距下课 / 30分钟` | `时长占比 / 82%` |
| `status_key` | active/upcoming/... | rank1/rank2/rank3/rank |

图片文件名：`rank_{start:%Y%m%d}_{end:%Y%m%d}.png`。同一区间内有人改课表时内容会变，但 `_asset_temp_path` 每次都会被重新读取上传，不需要额外指纹（如果以后发现客户端按路径缓存，再加 `max(revision)` 即可）。

### 4.4 `main.py`：命令 + AI 工具 + Web API

**命令**（已实现）

```python
@filter.command("上课时长榜", alias={"上课排行", "本周上课排行", "学习时长榜"})
async def rank_board(self, event: AstrMessageEvent, query: str = ""):
    """生成本会话群友上课时长排行榜，例如 /上课时长榜 或 /上课时长榜 本月。"""
```

参数解析用新增的 `plugin/texts.py:_command_tail(event, value)`：AstrBot 对普通 `str` 形参只绑定命令后的**第一个词**，所以 `2026-09-01 .. 2026-09-30` 这种带空格的区间必须回退到原始消息文本才能拿全。`/课表` 原有的内联版本已一并改用该 helper。

```text
/上课时长榜            → 本周
/上课时长榜 今日
/上课时长榜 上月
/上课时长榜 2026-09-01..2026-09-30
```

会话内无课表、或该区间内无人有课，回 `event.plain_result("当前会话还没有可统计的课程。")`。区间无法识别或超过 366 天时，`_rank_board_rows` 抛 `ValueError`，命令层转成纯文本回复。

**服务层**（已实现，P3 的 Web API 直接复用）

```python
async def _rank_board_rows(self, event, period="") -> tuple[list[dict], str]:
    """返回 (榜单行, 区间文案)；区间非法或过宽时抛 ValueError。"""

async def _rank_board_image(self, event, period="") -> str | None:
    """渲染榜单图片；无可统计内容时返回 None。"""
```

统计与渲染都放在 `asyncio.to_thread` 里执行：`_expand_member_occurrences` 是同步 CPU 操作，`_fetch_avatar` 更是同步阻塞的 `urlopen`，留在事件循环里会卡住 AstrBot。`_group_schedule_image` 也一并做了同样的处理。

**AI 工具**：按决策 4 不新增。

**Web API**（P3，待实现；与现有 3 个 endpoint 并列注册）

```python
GET /astrbot_plugin_course_schedule/rank?scope_id=&period=&limit=
→ {"scope_id", "label", "start", "end", "metric", "generated_at",
   "rows": [{"rank","user_id","name","minutes","hours_text","course_count",
             "course_names","elapsed_minutes","progress"}]}
```

数据源直接复用 `_rank_rows(scope_id, ...)`，与命令、工具同源。

### 4.5 Web 页面（可选，Phase 3）

两条路线，建议先做轻的：

- **轻**：在现有 `pages/schedule-manager` 的侧栏顶部加一个"本周排行"折叠面板，点开拉 `/rank` 渲染条形列表。改动集中在 `app.js` + `style.css`，不新增页面、不新增 i18n 条目。
- **重**：另开 `pages/rank-board`，需要补 `.astrbot-plugin/i18n/zh-CN.json` 的 `pages.rank-board` 条目。

### 4.6 插件配置（可选，Phase 4）

新增 `_conf_schema.json`（项目此前没有，属新增而非改动）：

```json
{
  "rank_metric":            {"type": "string", "default": "union", "options": ["union", "sum"],
                             "description": "时长口径：union 合并重叠课程，sum 直接累加"},
  "rank_top_n":             {"type": "int",    "default": 20,  "description": "图片榜单展示人数"},
  "rank_default_period":    {"type": "string", "default": "本周"},
  "rank_include_all_day":   {"type": "bool",   "default": false, "description": "全天事件是否计入时长"},
  "rank_exclude_keywords":  {"type": "list",   "default": [],  "description": "不计入时长的课程名关键词，如 自习、考试"},
  "rank_max_range_days":    {"type": "int",    "default": 366}
}
```

前 3 项建议先硬编码常量（`plugin/constants.py`），Phase 4 再落配置，避免一上来就引入配置面。

---

## 5. 性能与正确性

**性能**

- 单次统计成本 = 成员数 × 事件数 × RRULE 展开，`MAX_EVENTS_PER_FILE = 120` 给了天然上界；50 人 × 30 事件量级完全可接受。
- 但 `_expand_member_occurrences`、`_fetch_avatar` 都是同步 CPU/网络操作，**必须放进 `asyncio.to_thread`**，否则会阻塞 AstrBot 事件循环（现有 `_group_schedule_image` 也有这个问题，可一并修）。
- 可选加一层进程内缓存：键 `(scope_id, start_iso, end_iso, metric, tuple(sorted((uid, revision))))`，写入即失效，LRU 32 条 + TTL 300s。因为键里含 revision，天然不会读到脏数据。**Phase 1 不做**，等真出现性能反馈再加。

**正确性清单**

| 场景 | 期望 |
| --- | --- |
| 同一时段两门课 | `union` 只算一次；`sum` 算两次（口径差异有测例覆盖） |
| 23:00–01:00 跨天课，窗口=今日 | 只算 1 小时，不溢出到边界外 |
| 跨周日/周一的课，窗口=本周 | 只算落在本周内的部分 |
| DATE-only 全天事件 | 不计入时长，`all_day_count` 计数并在行内注明 |
| RRULE=FREQ=WEEKLY 无 UNTIL | 在窗口内每周计一次；窗口超 `rank_max_range_days` 时提示而不是硬算 |
| 无 `DTEND` 的事件 | 沿用 `_event_datetimes` 的 1.5 小时回退 |
| `end <= start` 的脏数据 | 沿用回退，不产生负时长 |
| 成员课表为空 / 未登记 | 默认不进榜；`include_empty` 时排末尾并标注 |
| 并列时长 | 同名次（1/2/2/4），排序稳定（按昵称、user_id 兜底） |
| 私聊会话 | 单榜，正常返回（列表只有一个人），不做特殊处理 |
| EXDATE 排除的课 | 不计入（`_expand_event_occurrences` 已处理） |

---

## 6. 测试情况

**`tests/test_rank.py`（已实现，13 个用例，纯函数、无需 stub AstrBot）**

复用 `tests/test_daily_schedule.py` 的模块加载方式，加载 `constants/ics/occurrences/domain/rank/sql_query`：

1. `test_union_metric_counts_overlapping_courses_once` —— 09:00-11:00 与 10:00-12:00，`union` 得 180 分钟，`sum` 得 240 分钟；
2. `test_clips_occurrences_to_the_window` —— 23:00–01:00 的课在单日窗口内只计 60 分钟；
3. `test_weekly_recurrence_counts_once_per_week` —— 周课在单周窗口 60 分钟 1 节，跨两周 120 分钟 2 节；
4. `test_all_day_events_are_excluded_but_reported` —— DATE-only 事件不计入但计入 `all_day_count`；`include_all_day=True` 时按并集计 24 小时（不叠加被它覆盖的课）；
5. `test_ties_share_a_position` —— 并列名次 1/2/2，排序稳定；
6. `test_members_without_courses_are_hidden_unless_requested` —— 默认隐藏，`include_empty=True` 时排末尾且 `rank=0`；
7. `test_elapsed_minutes_only_count_finished_courses` —— `now` 落在窗口中间时 `elapsed_minutes` 正确；
8. `test_progress_is_relative_to_the_leader` —— 榜首 1.0、次席 0.5；
9. `test_excluded_keywords_drop_matching_courses`；
10. `test_counts_repeated_course_as_one_name` —— 同一门课重复出现时 `course_count=2`、`course_names=1`；
11. `RankPeriodTests` —— 今日/本周/上周/本月/上月 的边界与文案、显式日期区间、非法区间报错。

**`tests/test_agent_tools.py`（已扩展，走真实 SQLite store）**

- `test_rank_board_orders_members_by_hours` —— 两个成员的 `_rank_board_rows` 名次、时长文案、占比，以及空窗口返回空；
- `test_rank_board_rejects_bad_and_overlong_periods` —— 非法区间与超 366 天都抛 `ValueError`；
- `test_command_tail_reads_past_the_bound_argument` —— 命令后带空格的区间能完整取出。

**未测**：图片渲染（现有测试也不渲染，依赖 Pillow + 字体，收益低）。改为用一张 5 人的样例数据手工渲染抽查，确认标题不再被省略、并列名次配色、`无课` 行与页脚文案都正确。

```text
.venv/bin/python -m pytest tests -q     →  32 passed
uvx ruff check .                        →  40 errors（与改动前基线一致，无新增）
```

---

## 7. 分期与进度

| Phase | 内容 | 状态 |
| --- | --- | --- |
| **P1 核心** | `plugin/rank.py`；`sql_query` 补 上周/上月；`render.py` 参数化 + 名次配色 + `_draw_rank_image`；`texts.py` 加 `_command_tail`；`main.py` 加 `/上课时长榜` 命令；统计与渲染走 `asyncio.to_thread` | ✅ 已完成 |
| **P2 AI** | `rank` LLM 工具 | ❌ 按决策 4 不做 |
| **P3 Web** | `GET /rank` JSON API + 课表管理页排行面板（`_rank_board_rows` 已就绪，直接复用） | 待做 |
| **P4 可选** | `_conf_schema.json`（`rank_metric` / `rank_top_n` / `rank_default_period` / `rank_include_all_day` / `rank_exclude_keywords` / `rank_max_range_days`）、进程内缓存、纯文本榜单、每周定时推送到群 | 待定 |

P1 已交付，且是后续所有出口的地基：P3 只是换壳调用同一个 `_rank_board_rows`，P4 的配置项直接喂给 `build_rank_rows` 已有的关键字参数（`metric` / `include_empty` / `include_all_day` / `exclude_keywords`），都不需要改统计内核。

## 8. 已定决策与遗留项

**已定**（2026-09-13）：

1. 统计窗口内的未上课**计入**，卡片同时显示"已上 X / 共 Y"（决策 3）；
2. **不**新增 AI `rank` 工具（决策 4）；
3. 时长口径用 `union`，撞课只算一次（决策 1）；
4. 命令主名 `/上课时长榜`，别名 `/上课排行`、`/本周上课排行`、`/学习时长榜`。

**遗留**（都不阻塞使用）：

- 图片默认只展示前 20 名（`DEFAULT_RANK_TOP_N`），尾部在页脚注明。人数多的大群可以调大，但 `_fetch_avatar` 是逐个同步拉取 QQ 头像，人数越多出图越慢。
- RRULE 无 `UNTIL` 的课会无限期重复，榜单在很久以后的区间仍会把它算进去；除 366 天上限外，根治办法是导入时补 `UNTIL` 或 `EXDATE`，这属于导入侧的独立议题。
- `include_empty`（把没课的成员也列出来）目前命令层没有开关，默认关闭。想在大群做"谁在摸鱼"的整活版，打开它即可（P4 配置项或加个命令参数）。
