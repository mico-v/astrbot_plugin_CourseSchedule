from __future__ import annotations

import os
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from functools import lru_cache
from pathlib import Path
from urllib.request import urlopen

from PIL import Image, ImageDraw, ImageFont

from .constants import FONT_DIR
from .files import _plugin_data_dir_path


_EMOJI_FONT_NAMES = (
    "QQEmoji.ttf",
    "QQEmoji.otf",
    "QEmoji.ttf",
    "TwemojiMozilla.ttf",
    "NotoColorEmoji.ttf",
    "NotoEmoji-VariableFont_wght.ttf",
    "NotoEmoji-Regular.ttf",
    "NotoEmoji.ttf",
    "seguiemj.ttf",
    "Apple Color Emoji.ttc",
    "Symbola.ttf",
)


def _font_candidates(bold: bool = False) -> list[str]:
    return [
        str(FONT_DIR / "NotoSansCJKsc-Bold.otf") if bold else "",
        str(FONT_DIR / "NotoSansCJKsc-Regular.otf"),
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc" if bold else "",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]


def _emoji_font_candidates() -> list[str]:
    roots = [
        FONT_DIR,
        Path("/usr/share/fonts/truetype/noto"),
        Path("/usr/share/fonts/opentype/noto"),
        Path("/usr/share/fonts/truetype/emoji"),
        Path("/usr/share/texmf-dist/fonts/truetype/google/noto-emoji"),
        Path("/usr/share/texmf-dist/fonts/truetype/public/twemoji-colr"),
        Path("/System/Library/Fonts"),
        Path("C:/Windows/Fonts"),
    ]
    return [str(root / name) for root in roots for name in _EMOJI_FONT_NAMES]


@lru_cache(maxsize=128)
def _load_font(
    size: int, bold: bool = False, emoji: bool = False
) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = _emoji_font_candidates() if emoji else _font_candidates(bold)
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            try:
                return ImageFont.truetype(candidate, size)
            except OSError:
                continue
    return ImageFont.load_default()


@lru_cache(maxsize=64)
def _bitmap_strike_size(font_path: str) -> int | None:
    """Native pixel size of an embedded-bitmap colour font (CBDT/sbix/EBDT).

    FreeType refuses any other size for bitmap-only fonts, raising
    ``OSError: invalid pixel size``, so the strike has to be discovered before
    a colour Emoji font can be used.
    """
    try:
        from fontTools.ttLib import TTFont

        with TTFont(font_path, lazy=True) as loaded:
            if "CBLC" in loaded:
                return int(loaded["CBLC"].strikes[0].bitmapSizeTable.ppemY)
            if "EBLC" in loaded:
                return int(loaded["EBLC"].strikes[0].bitmapSizeTable.ppemY)
            if "sbix" in loaded:
                return int(loaded["sbix"].strikes[0].ppem)
    except Exception:
        pass
    # fontTools unavailable: probe the sizes bitmap Emoji fonts commonly use.
    for probe in (109, 136, 128, 160, 96, 64):
        try:
            ImageFont.truetype(font_path, probe)
            return probe
        except OSError:
            continue
    return None


@lru_cache(maxsize=64)
def _emoji_font_variants(
    size: int,
) -> tuple[tuple[ImageFont.FreeTypeFont, float], ...]:
    """Load every installed Emoji font as ``(font, scale)``.

    Vector colour fonts (COLR/CPAL) load at the requested size with a scale of
    1.0.  Bitmap colour fonts (Noto Color Emoji CBDT) only accept their native
    strike, so they are loaded there and drawn scaled down to ``size``.
    """
    variants: list[tuple[ImageFont.FreeTypeFont, float]] = []
    seen: set[str] = set()
    for candidate in _emoji_font_candidates():
        if not candidate or not Path(candidate).exists():
            continue
        key = str(Path(candidate).resolve())
        if key in seen:
            continue
        seen.add(key)
        try:
            variants.append((ImageFont.truetype(candidate, size), 1.0))
            continue
        except OSError:
            pass
        strike = _bitmap_strike_size(candidate)
        if not strike:
            continue
        try:
            variants.append((ImageFont.truetype(candidate, strike), size / strike))
        except OSError:
            continue
    return tuple(variants)


def _has_real_font(font: ImageFont.ImageFont) -> bool:
    return isinstance(font, ImageFont.FreeTypeFont) and isinstance(
        getattr(font, "path", None), (str, Path)
    )


@lru_cache(maxsize=64)
def _font_codepoints(font_path: str, font_number: int) -> frozenset[int] | None:
    try:
        from fontTools.ttLib import TTFont

        with TTFont(font_path, lazy=True, fontNumber=font_number) as loaded:
            cmap: set[int] = set()
            for table in loaded["cmap"].tables:
                cmap.update(table.cmap)
            return frozenset(cmap)
    except Exception:
        return None


# A code point guaranteed to be absent from every real font, used to capture
# the .notdef (tofu) bitmap when fontTools cannot read the cmap.
_NOTDEF_SENTINEL = "\U0010ffff"


def _renders_notdef(font: ImageFont.ImageFont, text: str) -> bool:
    """Detect the .notdef/tofu glyph by comparing rendered bitmaps.

    ``getbbox`` treats the tofu box as a real box, so it cannot tell a missing
    glyph from a present one.  Rendering the same text as an unassigned code
    point and comparing the bitmaps identifies it without fontTools.
    """
    if not _has_real_font(font):
        return False
    try:
        probe = font.getmask(text)
        notdef = font.getmask(_NOTDEF_SENTINEL)
    except Exception:
        return False
    if not any(bytes(probe)):
        # An empty mask is inconclusive: COLR colour-layer glyphs and bitmap
        # fonts both report no monochrome coverage.  Do not call those tofu.
        return False
    return probe.size == notdef.size and bytes(probe) == bytes(notdef)


def _font_supports(font: ImageFont.ImageFont, text: str) -> bool:
    """Check cmap coverage so a missing glyph does not become a tofu box."""
    if not _has_real_font(font):
        return False
    font_path = str(getattr(font, "path", ""))
    cmap = _font_codepoints(font_path, getattr(font, "index", 0))
    if cmap is None:
        # fontTools is unavailable.  Never claim an Emoji is supported just
        # because it looks like one: verify per character that FreeType is not
        # falling back to the .notdef box, otherwise every Emoji would be
        # drawn with the CJK font as a tofu box.
        return all(
            not _renders_notdef(font, character)
            for character in text
            if unicodedata.category(character) != "Cf"
            and not unicodedata.category(character).startswith("M")
        )
    is_emoji_text = any(_is_emoji_character(character) for character in text)
    return all(
        ord(character) in cmap
        for character in text
        if unicodedata.category(character) != "Cf"
        and not unicodedata.category(character).startswith("M")
        and not (is_emoji_text and ord(character) < 0x80)
    )


def _font_has_visible_glyph(font: ImageFont.ImageFont, text: str) -> bool:
    if _renders_notdef(font, text):
        return False
    try:
        bbox = font.getbbox(text)
    except (AttributeError, ValueError):
        return False
    return bool(bbox and bbox[2] > bbox[0] and bbox[3] > bbox[1])


def _is_emoji_character(character: str) -> bool:
    codepoint = ord(character)
    return (
        0x1F000 <= codepoint <= 0x1FAFF
        or 0x1FC00 <= codepoint <= 0x1FFFD
        or 0x2600 <= codepoint <= 0x27BF
        or 0xFE0F == codepoint
        or 0xE000 <= codepoint <= 0xF8FF
        or 0xF0000 <= codepoint <= 0xFFFFD
        or 0x100000 <= codepoint <= 0x10FFFD
    )


def _is_emoji_cluster(cluster: str) -> bool:
    return any(_is_emoji_character(character) for character in cluster)


def _graphemes(text: str) -> list[str]:
    """Split text without breaking emoji ZWJ, flags, or skin-tone sequences."""
    clusters: list[str] = []
    current = ""
    regional_count = 0
    for character in str(text or ""):
        codepoint = ord(character)
        combining = unicodedata.category(character).startswith("M")
        if not current:
            current = character
            regional_count = 1 if 0x1F1E6 <= codepoint <= 0x1F1FF else 0
            continue

        join_current = (
            character in ("\ufe0f", "\u20e3")
            or 0x1F3FB <= codepoint <= 0x1F3FF
            or character == "\u200d"
            or current.endswith("\u200d")
            or combining
        )
        if 0x1F1E6 <= codepoint <= 0x1F1FF:
            join_current = regional_count == 1
            regional_count = 2 if join_current else 1
        else:
            regional_count = 0
        if join_current:
            current += character
        else:
            clusters.append(current)
            current = character
    if current:
        clusters.append(current)
    return clusters


@lru_cache(maxsize=16384)
def _font_for_cluster(
    cluster: str, size: int, bold: bool
) -> tuple[ImageFont.ImageFont, bool, float]:
    """Return ``(font, is_emoji, scale)`` for one grapheme cluster.

    ``scale`` is 1.0 for outline fonts and < 1.0 when the glyph comes from a
    bitmap Emoji font rendered at its native strike and downscaled.
    """
    primary = _load_font(size, bold=bold)
    if not _is_emoji_cluster(cluster):
        return primary, False, 1.0

    if _font_supports(primary, cluster) and _font_has_visible_glyph(primary, cluster):
        return primary, False, 1.0

    # Keep one consistent primary font for all regular text.  Only a cluster
    # that the primary font cannot render gets an Emoji fallback (important for
    # QQ private-use Emoji and ZWJ sequences).  Try every installed Emoji font
    # and keep the first that really covers this cluster, so an outdated
    # bundled font does not shadow a fuller system font.
    variants = _emoji_font_variants(size)
    for emoji_font, scale in variants:
        if _font_supports(emoji_font, cluster) and _font_has_visible_glyph(
            emoji_font, cluster
        ):
            return emoji_font, True, scale

    # No installed font has the glyph.  A dedicated Emoji font still renders a
    # better-sized fallback than the CJK primary, so prefer it when available.
    if variants:
        emoji_font, scale = variants[0]
        return emoji_font, True, scale
    return primary, False, 1.0


@lru_cache(maxsize=16384)
def _cluster_advance(cluster: str, size: int, bold: bool) -> float:
    font, _is_emoji, scale = _font_for_cluster(cluster, size, bold)
    return font.getlength(cluster) * scale


def _rich_width(text: str, size: int, bold: bool = False) -> float:
    return sum(_cluster_advance(cluster, size, bold) for cluster in _graphemes(text))


def _fit_rich_text(text: str, size: int, max_width: int, bold: bool = False) -> str:
    raw = str(text or "")
    if _rich_width(raw, size, bold) <= max_width:
        return raw
    suffix = "…"
    suffix_width = _rich_width(suffix, size, bold)
    fitted: list[str] = []
    used = 0.0
    for cluster in _graphemes(raw):
        cluster_width = _rich_width(cluster, size, bold)
        if used + cluster_width + suffix_width > max_width:
            break
        fitted.append(cluster)
        used += cluster_width
    return "".join(fitted) + suffix if fitted else suffix


def _wrap_rich_text(text: str, size: int, max_width: int, bold: bool = False) -> list[str]:
    """Wrap by grapheme cluster so names never split a combined Emoji."""
    lines: list[str] = []
    line: list[str] = []
    line_width = 0.0
    for cluster in _graphemes(str(text or "")):
        if cluster == "\n":
            lines.append("".join(line))
            line = []
            line_width = 0.0
            continue
        cluster_width = _rich_width(cluster, size, bold)
        if line and line_width + cluster_width > max_width:
            lines.append("".join(line))
            line = []
            line_width = 0.0
        line.append(cluster)
        line_width += cluster_width
    if line or not lines:
        lines.append("".join(line))
    return lines


def _baseline_for_top(font: ImageFont.ImageFont, top: float) -> float:
    """Convert a visual top coordinate to a shared alphabetic baseline."""
    bbox = font.getbbox("Ag中", anchor="ls")
    return top - bbox[1]


def _draw_scaled_emoji(
    image: Image.Image | None,
    x: float,
    baseline: float,
    cluster: str,
    font: ImageFont.FreeTypeFont,
    scale: float,
    fill: str,
) -> bool:
    """Draw a bitmap-strike Emoji glyph downscaled to the target size.

    FreeType only accepts the native strike size for CBDT/sbix fonts, so the
    glyph is rendered colour at that size and then resized.  Returns ``False``
    when no image is available or the glyph renders empty so the caller can
    fall back to a normal draw.
    """
    native = int(round(getattr(font, "size", 0) or 0))
    if image is None or native <= 0 or scale <= 0:
        return False
    pad = 4
    width = max(1, int(font.getlength(cluster)) + pad * 2)
    canvas = Image.new("RGBA", (width, native * 2), (0, 0, 0, 0))
    ImageDraw.Draw(canvas).text(
        (pad, native),
        cluster,
        font=font,
        fill=fill,
        anchor="ls",
        embedded_color=True,
    )
    box = canvas.getbbox()
    if box is None:
        return False
    scaled = canvas.crop(box).resize(
        (max(1, round((box[2] - box[0]) * scale)), max(1, round((box[3] - box[1]) * scale))),
        Image.LANCZOS,
    )
    paste_x = int(round(x + (box[0] - pad) * scale))
    paste_y = int(round(baseline + (box[1] - native) * scale))
    image.paste(scaled, (paste_x, paste_y), scaled)
    return True


def _draw_rich_text(
    draw: ImageDraw.ImageDraw,
    xy: tuple[float, float],
    text: str,
    size: int,
    fill: str,
    *,
    bold: bool = False,
    max_width: int | None = None,
) -> float:
    value = _fit_rich_text(text, size, max_width, bold) if max_width else str(text or "")
    x, y = xy
    baseline = _baseline_for_top(_load_font(size, bold=bold), y)
    image = getattr(draw, "_image", None)
    for cluster in _graphemes(value):
        font, is_emoji, scale = _font_for_cluster(cluster, size, bold)
        if is_emoji and scale != 1.0 and _draw_scaled_emoji(
            image, x, baseline, cluster, font, scale, fill
        ):
            x += font.getlength(cluster) * scale
            continue
        try:
            draw.text(
                (x, baseline),
                cluster,
                font=font,
                fill=fill,
                anchor="ls",
                embedded_color=is_emoji,
            )
        except TypeError:
            # Pillow versions before embedded_color still render monochrome
            # emoji fonts, which is preferable to dropping the nickname glyph.
            draw.text((x, baseline), cluster, font=font, fill=fill, anchor="ls")
        x += font.getlength(cluster) * scale
    return x


def _draw_wrapped_rich_text(
    draw: ImageDraw.ImageDraw,
    xy: tuple[float, float],
    text: str,
    size: int,
    fill: str,
    max_width: int,
    *,
    bold: bool = False,
    line_height: int | None = None,
) -> int:
    lines = _wrap_rich_text(text, size, max_width, bold)
    step = line_height or size + 8
    for index, line in enumerate(lines):
        _draw_rich_text(draw, (xy[0], xy[1] + index * step), line, size, fill, bold=bold)
    return len(lines)


def _ellipsis(text: str, font: ImageFont.ImageFont, max_width: int) -> str:
    """Compatibility helper retained for callers using the old renderer API."""
    if font.getlength(text) <= max_width:
        return text
    suffix = "..."
    while text and font.getlength(text + suffix) > max_width:
        text = text[:-1]
    return text + suffix if text else suffix


def _circle_avatar(avatar: Image.Image, size: int) -> Image.Image:
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size - 1, size - 1), fill=255)
    rounded = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    rounded.paste(avatar, (0, 0), mask)
    return rounded


def _download_avatar(user_id: str, size: int) -> Image.Image | None:
    url = f"https://q1.qlogo.cn/g?b=qq&nk={user_id}&s=100"
    try:
        with urlopen(url, timeout=5) as response:
            avatar = Image.open(response).convert("RGB").resize((size, size))
    except Exception:
        return None
    return _circle_avatar(avatar, size)


def _fallback_avatar(user_id: str, size: int) -> Image.Image:
    avatar = Image.new("RGB", (size, size), "#dbe4f0")
    font = _load_font(22, bold=True)
    label = user_id[-2:] if user_id else "?"
    ImageDraw.Draw(avatar).text(
        (size / 2, size / 2), label, fill="#40516b", font=font, anchor="mm"
    )
    return _circle_avatar(avatar, size)


def _read_avatar_cache(path: Path) -> Image.Image | None:
    """Return the cached avatar when it is younger than the TTL."""
    try:
        stats = path.stat()
    except OSError:
        return None
    if time.time() - stats.st_mtime > AVATAR_CACHE_TTL_SECONDS:
        return None
    try:
        with Image.open(path) as cached:
            return cached.convert("RGBA")
    except Exception:
        return None


def _write_avatar_cache(path: Path, avatar: Image.Image) -> None:
    """Persist an avatar atomically so a killed process cannot leave a partial file."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
        avatar.save(tmp, format="PNG")
        os.replace(tmp, path)
    except Exception:
        pass


@lru_cache(maxsize=512)
def _fetch_avatar(user_id: str, size: int) -> Image.Image:
    """Fetch one avatar, preferring a disk cache that expires after one day."""
    cache_path = _avatar_cache_path(user_id, size)
    cached = _read_avatar_cache(cache_path)
    if cached is not None:
        return cached
    avatar = _download_avatar(user_id, size)
    if avatar is not None:
        _write_avatar_cache(cache_path, avatar)
        return avatar
    return _fallback_avatar(user_id, size)


# Avatar downloads are network bound and were fetched one by one, so a group
# of N members paid N sequential round trips (and N timeouts when qlogo is
# unreachable).  Fetch them concurrently first; _fetch_avatar is cached, so the
# per-row calls during drawing become cache hits.
AVATAR_SIZE = 76
_AVATAR_WORKERS = 8
AVATAR_CACHE_TTL_SECONDS = 24 * 60 * 60


# Output format: JPEG keeps the encoder fast (several times quicker than PNG on
# these mostly-flat cards) at a comparable payload size.  4:2:0 subsampling is
# fine for this content and keeps the file small.
IMAGE_EXTENSION = ".jpg"
IMAGE_JPEG_QUALITY = 85
IMAGE_JPEG_SUBSAMPLING = 2

# Generated cards are only needed until the platform has fetched them, so old
# files in the output directory are pruned instead of growing forever.
OUTPUT_MAX_AGE_SECONDS = 24 * 60 * 60


@lru_cache(maxsize=1)
def _avatar_cache_dir() -> Path:
    return _plugin_data_dir_path("avatars")


def _avatar_cache_path(user_id: str, size: int) -> Path:
    safe = "".join(
        character if character.isalnum() or character in "._-" else "_"
        for character in user_id
    )[:64]
    return _avatar_cache_dir() / f"{safe or 'unknown'}_{size}.png"


def _prune_old_outputs(directory: Path) -> None:
    now = time.time()
    try:
        for item in directory.iterdir():
            if item.is_file() and now - item.stat().st_mtime > OUTPUT_MAX_AGE_SECONDS:
                item.unlink(missing_ok=True)
    except OSError:
        pass


def _save_output_image(image: Image.Image, filename: str) -> str:
    """Save the rendered card and return its path in the configured format."""
    directory = _plugin_data_dir_path("images")
    _prune_old_outputs(directory)
    path = directory / Path(filename).with_suffix(IMAGE_EXTENSION).name
    image.convert("RGB").save(
        path,
        format="JPEG",
        quality=IMAGE_JPEG_QUALITY,
        subsampling=IMAGE_JPEG_SUBSAMPLING,
    )
    return str(path)


def _prefetch_avatars(user_ids: list[object], size: int) -> None:
    unique = [uid for uid in dict.fromkeys(str(uid or "") for uid in user_ids) if uid]
    if len(unique) <= 1:
        return
    workers = min(_AVATAR_WORKERS, len(unique))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(lambda uid: _fetch_avatar(uid, size), unique))


def _status_colors(status_key: str) -> tuple[str, str, str]:
    return {
        "active": ("#0f766e", "#ccfbf1", "#14b8a6"),
        "upcoming": ("#2563eb", "#dbeafe", "#60a5fa"),
        "finished": ("#64748b", "#f1f5f9", "#94a3b8"),
        "scheduled": ("#7c3aed", "#ede9fe", "#a78bfa"),
        "holiday": ("#b45309", "#fef3c7", "#f59e0b"),
        "none": ("#64748b", "#f8fafc", "#cbd5e1"),
        "rank1": ("#b45309", "#fef3c7", "#f59e0b"),
        "rank2": ("#475569", "#e2e8f0", "#94a3b8"),
        "rank3": ("#9a3412", "#ffedd5", "#fb923c"),
        "rank": ("#1d4ed8", "#dbeafe", "#60a5fa"),
    }.get(status_key, ("#475569", "#f1f5f9", "#94a3b8"))


def _rank_status_key(rank: int) -> str:
    if rank <= 0:
        return "none"
    return "rank" if rank > 3 else f"rank{rank}"


def _rank_card(row: dict[str, object]) -> dict[str, object]:
    """Map one ranking row onto the shared course-card layout."""
    rank = int(row.get("rank") or 0)
    if rank:
        duration = (
            f"已上 {row.get('elapsed_text') or '0分钟'}"
            f" / 共 {row.get('hours_text') or '0分钟'}"
        )
        countdown = f"{round(float(row.get('progress') or 0.0) * 100)}%"
    else:
        duration = "统计区间内没有课程"
        countdown = "—"
    return {
        "user_id": str(row.get("user_id") or ""),
        "name": str(row.get("name") or row.get("user_id") or "未知成员"),
        "status_key": _rank_status_key(rank),
        "status": f"#{rank}" if rank else "无课",
        "course": str(row.get("hours_text") or "0分钟"),
        "time": (
            f"{int(row.get('course_count') or 0)} 节 · "
            f"{int(row.get('course_names') or 0)} 门课"
        ),
        "duration": duration,
        "progress": float(row.get("progress") or 0.0),
        "countdown_label": "时长占比",
        "countdown": countdown,
    }


def _draw_rank_image(
    title: str,
    rows: list[dict[str, object]],
    filename: str,
    *,
    subtitle: str,
    footer: str,
    top_n: int = 0,
    started_at: float | None = None,
) -> str:
    """Render a class-hours leaderboard on the shared member-card layout."""
    shown = rows[:top_n] if top_n and len(rows) > top_n else rows
    return _draw_rows_image(
        title,
        [_rank_card(row) for row in shown],
        filename,
        subtitle=subtitle,
        legend=[
            ("none", "同一时段冲突的课程只计一次"),
            ("none", "全天日程不计入时长"),
        ],
        duration_label="",
        footer=footer,
        started_at=started_at,
    )


def _draw_badge(
    draw: ImageDraw.ImageDraw,
    left: int,
    top: int,
    text: str,
    font: ImageFont.ImageFont,
    foreground: str,
    background: str,
) -> int:
    padding_x = 16
    width = int(font.getlength(text)) + padding_x * 2
    height = 34
    draw.rounded_rectangle((left, top, left + width, top + height), radius=17, fill=background)
    _draw_rich_text(
        draw,
        (left + padding_x, top + 5),
        text,
        int(getattr(font, "size", 16)),
        foreground,
        bold=True,
    )
    return width


def _draw_progress(
    draw: ImageDraw.ImageDraw,
    left: int,
    top: int,
    width: int,
    progress: float,
    color: str,
) -> None:
    height = 8
    draw.rounded_rectangle((left, top, left + width, top + height), radius=4, fill="#e2e8f0")
    fill_width = max(8 if progress > 0 else 0, int(width * min(1.0, max(0.0, progress))))
    if fill_width:
        draw.rounded_rectangle(
            (left, top, left + fill_width, top + height), radius=4, fill=color
        )


FOOTER_LIVE = "实时状态 · 课程时间以本地时区为准"
FOOTER_ARCHIVE = "历史课表 · 课程时间以本地时区为准"
FOOTER_PLANNED = "课程安排 · 课程时间以本地时区为准"


def schedule_footer(selected: date, today: date) -> str:
    """Footer line for one day view, so a past day is not called "live"."""
    if selected < today:
        return FOOTER_ARCHIVE
    if selected > today:
        return FOOTER_PLANNED
    return FOOTER_LIVE


def _draw_rows_image(
    title: str,
    rows: list[dict[str, object]],
    filename: str,
    *,
    subtitle: str | None = None,
    legend: list[tuple[str, str]] | None = None,
    duration_label: str = "本节持续",
    footer: str = FOOTER_LIVE,
    started_at: float | None = None,
) -> str:
    width = 1240
    header_height = 202
    card_gap = 16
    footer_height = 54
    name_width = 235
    name_line_height = 30
    name_lines = [
        len(_wrap_rich_text(str(row.get("name") or row.get("user_id") or "未知成员"), 27, name_width, True))
        for row in rows
    ]
    card_heights = [
        max(156, 100 + max(0, line_count - 1) * name_line_height)
        for line_count in name_lines
    ]
    cards_height = sum(card_heights) + card_gap * max(len(rows) - 1, 0)
    height = max(
        360,
        header_height
        + (cards_height if rows else 140)
        + footer_height,
    )
    image = Image.new("RGB", (width, height), "#f5f7fc")
    draw = ImageDraw.Draw(image)

    body_font = _load_font(20)
    small_font = _load_font(17)
    badge_font = _load_font(16, bold=True)

    header_top = (30, 41, 72)
    header_bottom = (48, 73, 116)
    for y in range(header_height):
        ratio = y / max(1, header_height - 1)
        color = tuple(
            int(header_top[index] + (header_bottom[index] - header_top[index]) * ratio)
            for index in range(3)
        )
        draw.line((0, y, width, y), fill=color)
    draw.ellipse((width - 190, -105, width + 70, 155), fill="#496a9d")
    draw.ellipse((width - 90, 70, width + 90, 250), fill="#3e5b8d")
    _draw_rich_text(draw, (42, 32), title, 40, "#ffffff", bold=True, max_width=760)
    if subtitle is None:
        active_count = sum(row.get("status_key") == "active" for row in rows)
        upcoming_count = sum(
            row.get("status_key") in ("upcoming", "scheduled") for row in rows
        )
        subtitle = (
            f"共 {len(rows)} 位成员  ·  {active_count} 人正在上课  ·  "
            f"{upcoming_count} 人待上课"
        )
    _draw_rich_text(draw, (44, 92), subtitle, 20, "#dbeafe")

    legend_top = 143
    legend_left = 44
    if legend is None:
        legend_items = [
            ("active", "正在上课"),
            ("upcoming", "下一节即将上"),
            ("finished", "今日已结束"),
        ]
        if any(row.get("status_key") == "holiday" for row in rows):
            legend_items.append(("holiday", "休假"))
        if any(row.get("override_note") for row in rows):
            legend_items.append(("scheduled", "调休上课"))
    else:
        legend_items = list(legend)
    for status_key, label in legend_items:
        _foreground, _background, accent = _status_colors(status_key)
        draw.ellipse((legend_left, legend_top + 10, legend_left + 10, legend_top + 20), fill=accent)
        _draw_rich_text(draw, (legend_left + 18, legend_top), label, 17, "#e2e8f0")
        legend_left += int(_rich_width(label, 17)) + 58

    if not rows:
        draw.rounded_rectangle((36, header_height, width - 36, header_height + 140), radius=22, fill="#ffffff")
        draw.text((width / 2, header_height + 70), "暂无成员课程数据", fill="#64748b", font=body_font, anchor="mm")

    _prefetch_avatars([row.get("user_id") for row in rows], AVATAR_SIZE)

    for index, row in enumerate(rows):
        top = header_height + sum(card_heights[:index]) + index * card_gap
        card_height = card_heights[index]
        left = 36
        right = width - 36
        status_key = str(row.get("status_key") or "none")
        foreground, badge_background, accent = _status_colors(status_key)
        card_fill = "#ffffff" if index % 2 == 0 else "#fcfdff"
        draw.rounded_rectangle((left, top, right, top + card_height), radius=22, fill=card_fill)
        draw.rounded_rectangle((left, top, left + 8, top + card_height), radius=4, fill=accent)

        avatar = _fetch_avatar(str(row.get("user_id") or ""), AVATAR_SIZE)
        image.paste(avatar, (62, top + 40), avatar)
        _draw_wrapped_rich_text(
            draw,
            (158, top + 31),
            str(row.get("name") or row.get("user_id") or "未知成员"),
            27,
            "#17233c",
            name_width,
            bold=True,
            line_height=name_line_height,
        )
        _draw_rich_text(
            draw,
            (158, top + 76 + max(0, name_lines[index] - 1) * name_line_height),
            str(row.get("user_id") or ""),
            17,
            "#94a3b8",
        )

        _draw_rich_text(
            draw,
            (430, top + 25),
            str(row.get("course") or "暂无课程安排"),
            26,
            "#17233c" if status_key != "none" else "#64748b",
            bold=True,
            max_width=470,
        )
        time_text = str(row.get("time") or "")
        location = str(row.get("location") or "").strip()
        if location:
            time_text += f"   ·   {location}"
        _draw_rich_text(draw, (430, top + 66), time_text, 19, "#64748b", max_width=480)
        duration = str(row.get("duration") or "—")
        duration_text = f"{duration_label} {duration}".strip()
        _draw_rich_text(draw, (430, top + 101), duration_text, 17, "#94a3b8", max_width=480)
        _draw_progress(draw, 430, top + 132, 480, float(row.get("progress") or 0), accent)

        badge_text = str(row.get("status") or "")
        _draw_badge(draw, 972, top + 24, badge_text, badge_font, foreground, badge_background)
        countdown_label = str(row.get("countdown_label") or "")
        countdown = str(row.get("countdown") or "")
        _draw_rich_text(draw, (972, top + 76), countdown_label, 17, "#94a3b8")
        _draw_rich_text(
            draw,
            (972, top + 96),
            countdown,
            20,
            foreground,
            bold=True,
            max_width=right - 972 - 24,
        )

    footer_top = header_height + (cards_height if rows else 140)
    if started_at is not None:
        elapsed_ms = (time.perf_counter() - started_at) * 1000
        footer = f"{footer}  ·  生成耗时 {elapsed_ms:.0f} ms"
    draw.text(
        (width / 2, footer_top + 22),
        footer,
        font=small_font,
        fill="#94a3b8",
        anchor="mm",
    )

    return _save_output_image(image, filename)
