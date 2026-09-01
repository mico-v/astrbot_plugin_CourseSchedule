from __future__ import annotations

import unicodedata
from functools import lru_cache
from pathlib import Path
from urllib.request import urlopen

from PIL import Image, ImageDraw, ImageFont

from .constants import FONT_DIR
from .files import _asset_temp_path


_EMOJI_FONT_NAMES = (
    "QQEmoji.ttf",
    "QQEmoji.otf",
    "QEmoji.ttf",
    "TwemojiMozilla.ttf",
    "NotoColorEmoji.ttf",
    "NotoEmoji-Regular.ttf",
    "NotoEmoji-VariableFont_wght.ttf",
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


def _font_supports(font: ImageFont.ImageFont, text: str) -> bool:
    """Check cmap coverage so a missing glyph does not become a tofu box."""
    if not _has_real_font(font):
        return False
    font_path = str(getattr(font, "path", ""))
    cmap = _font_codepoints(font_path, getattr(font, "index", 0))
    if cmap is None:
        # If fontTools is not installed, keep the fallback useful for fonts
        # selected specifically as emoji fonts.
        return any(_is_emoji_character(character) for character in text)
    is_emoji_text = any(_is_emoji_character(character) for character in text)
    return all(
        ord(character) in cmap
        for character in text
        if unicodedata.category(character) != "Cf"
        and not unicodedata.category(character).startswith("M")
        and not (is_emoji_text and ord(character) < 0x80)
    )


def _font_has_visible_glyph(font: ImageFont.ImageFont, text: str) -> bool:
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


def _font_for_cluster(
    cluster: str, size: int, bold: bool
) -> tuple[ImageFont.ImageFont, bool]:
    primary = _load_font(size, bold=bold)
    if not _is_emoji_cluster(cluster):
        return primary, False

    if _font_supports(primary, cluster) and _font_has_visible_glyph(primary, cluster):
        return primary, False

    # Keep one consistent primary font for all regular text.  Only a cluster
    # that the primary font cannot render gets the single configured Emoji
    # fallback (important for QQ private-use Emoji and ZWJ sequences).
    emoji_font = _load_font(size, emoji=True)
    if _font_supports(emoji_font, cluster) and _font_has_visible_glyph(emoji_font, cluster):
        return emoji_font, True
    return primary, False


def _rich_width(text: str, size: int, bold: bool = False) -> float:
    return sum(
        _font_for_cluster(cluster, size, bold)[0].getlength(cluster)
        for cluster in _graphemes(text)
    )


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
    for cluster in _graphemes(value):
        font, is_emoji = _font_for_cluster(cluster, size, bold)
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
        x += font.getlength(cluster)
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


@lru_cache(maxsize=256)
def _fetch_avatar(user_id: str, size: int) -> Image.Image:
    url = f"https://q1.qlogo.cn/g?b=qq&nk={user_id}&s=100"
    try:
        with urlopen(url, timeout=5) as response:
            avatar = Image.open(response).convert("RGB").resize((size, size))
    except Exception:
        avatar = Image.new("RGB", (size, size), "#dbe4f0")
        fallback_draw = ImageDraw.Draw(avatar)
        font = _load_font(22, bold=True)
        label = user_id[-2:] if user_id else "?"
        fallback_draw.text((size / 2, size / 2), label, fill="#40516b", font=font, anchor="mm")

    mask = Image.new("L", (size, size), 0)
    mask_draw = ImageDraw.Draw(mask)
    mask_draw.ellipse((0, 0, size - 1, size - 1), fill=255)
    rounded = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    rounded.paste(avatar, (0, 0), mask)
    return rounded


def _status_colors(status_key: str) -> tuple[str, str, str]:
    return {
        "active": ("#0f766e", "#ccfbf1", "#14b8a6"),
        "upcoming": ("#2563eb", "#dbeafe", "#60a5fa"),
        "finished": ("#64748b", "#f1f5f9", "#94a3b8"),
        "scheduled": ("#7c3aed", "#ede9fe", "#a78bfa"),
        "none": ("#64748b", "#f8fafc", "#cbd5e1"),
    }.get(status_key, ("#475569", "#f1f5f9", "#94a3b8"))


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


def _draw_rows_image(title: str, rows: list[dict[str, object]], filename: str) -> str:
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
    active_count = sum(row.get("status_key") == "active" for row in rows)
    upcoming_count = sum(row.get("status_key") in ("upcoming", "scheduled") for row in rows)
    subtitle = f"共 {len(rows)} 位成员  ·  {active_count} 人正在上课  ·  {upcoming_count} 人待上课"
    _draw_rich_text(draw, (44, 92), subtitle, 20, "#dbeafe")

    legend_top = 143
    legend_left = 44
    for status_key, label in (
        ("active", "正在上课"),
        ("upcoming", "下一节即将上"),
        ("finished", "今日已结束"),
    ):
        _foreground, _background, accent = _status_colors(status_key)
        draw.ellipse((legend_left, legend_top + 10, legend_left + 10, legend_top + 20), fill=accent)
        _draw_rich_text(draw, (legend_left + 18, legend_top), label, 17, "#e2e8f0")
        legend_left += int(_rich_width(label, 17)) + 58

    if not rows:
        draw.rounded_rectangle((36, header_height, width - 36, header_height + 140), radius=22, fill="#ffffff")
        draw.text((width / 2, header_height + 70), "暂无成员课程数据", fill="#64748b", font=body_font, anchor="mm")

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

        avatar = _fetch_avatar(str(row.get("user_id") or ""), 76)
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
        _draw_rich_text(draw, (430, top + 101), f"本节持续 {duration}", 17, "#94a3b8", max_width=480)
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
    draw.text(
        (width / 2, footer_top + 22),
        "实时状态 · 课程时间以本地时区为准",
        font=small_font,
        fill="#94a3b8",
        anchor="mm",
    )

    path = _asset_temp_path(filename)
    image.save(path)
    return path
