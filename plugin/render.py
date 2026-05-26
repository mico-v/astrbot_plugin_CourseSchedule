from __future__ import annotations

from pathlib import Path
from urllib.request import urlopen

from PIL import Image, ImageDraw, ImageFont

from .constants import FONT_DIR
from .files import _asset_temp_path


def _load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = [
        str(FONT_DIR / "NotoSansCJKsc-Bold.otf") if bold else "",
        str(FONT_DIR / "NotoSansCJKsc-Regular.otf"),
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc" if bold else "",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return ImageFont.truetype(candidate, size)
    return ImageFont.load_default()


def _ellipsis(text: str, font: ImageFont.ImageFont, max_width: int) -> str:
    if font.getlength(text) <= max_width:
        return text

    suffix = "..."
    while text and font.getlength(text + suffix) > max_width:
        text = text[:-1]
    return text + suffix if text else suffix


def _fetch_avatar(user_id: str, size: int) -> Image.Image:
    url = f"https://q1.qlogo.cn/g?b=qq&nk={user_id}&s=100"
    try:
        with urlopen(url, timeout=5) as response:
            avatar = Image.open(response).convert("RGB").resize((size, size))
    except Exception:
        avatar = Image.new("RGB", (size, size), "#d8dee9")
        draw = ImageDraw.Draw(avatar)
        font = _load_font(20, bold=True)
        label = user_id[-2:] if user_id else "?"
        draw.text((size / 2, size / 2), label, fill="#3b4252", font=font, anchor="mm")

    mask = Image.new("L", (size, size), 0)
    draw = ImageDraw.Draw(mask)
    draw.ellipse((0, 0, size - 1, size - 1), fill=255)
    rounded = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    rounded.paste(avatar, (0, 0), mask)
    return rounded


def _draw_rows_image(title: str, rows: list[dict[str, str]], filename: str) -> str:
    width = 1100
    row_height = 104
    header_height = 116
    footer_height = 28
    height = max(260, header_height + row_height * max(len(rows), 1) + footer_height)
    image = Image.new("RGB", (width, height), "#f5f7fb")
    draw = ImageDraw.Draw(image)
    title_font = _load_font(36, bold=True)
    name_font = _load_font(24, bold=True)
    body_font = _load_font(22)
    small_font = _load_font(18)

    draw.rectangle((0, 0, width, 92), fill="#263238")
    draw.text((36, 46), title, fill="#ffffff", font=title_font, anchor="lm")

    if not rows:
        draw.text((width / 2, height / 2), "暂无课程数据", fill="#607d8b", font=body_font, anchor="mm")
    for index, row in enumerate(rows):
        top = header_height + index * row_height
        left = 30
        right = width - 30
        fill = "#ffffff" if index % 2 == 0 else "#eef3f8"
        draw.rounded_rectangle((left, top, right, top + 86), radius=10, fill=fill)

        avatar = _fetch_avatar(row["user_id"], 58)
        image.paste(avatar, (left + 20, top + 14), avatar)
        draw.text((left + 92, top + 26), _ellipsis(row["name"], name_font, 240), fill="#263238", font=name_font)
        draw.text((left + 92, top + 58), row["subtitle"], fill="#607d8b", font=small_font)

        status_color = "#2e7d32" if row.get("status") == "正在上" else "#1565c0"
        draw.text((left + 365, top + 24), row.get("status", ""), fill=status_color, font=name_font)
        draw.text(
            (left + 470, top + 24),
            _ellipsis(row["course"], body_font, 510),
            fill="#263238",
            font=body_font,
        )
        draw.text((left + 470, top + 58), row["time"], fill="#455a64", font=small_font)

    path = _asset_temp_path(filename)
    image.save(path)
    return path
