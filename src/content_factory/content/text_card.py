"""Типографская карточка: крупная цифра и подпись вместо сгенерированной фотографии.

Лента, где у каждого поста фотореалистичный кадр в одном стиле, выглядит
однообразно. Карточка «цифра дня» рисуется детерминированно и без квоты
генератора: шрифт и палитра фиксированы, поэтому результат всегда одинаков.
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

SIZE = 1080
BACKGROUND = (18, 42, 62)
ACCENT = (255, 196, 61)
TEXT = (236, 242, 247)
MUTED = (150, 172, 190)

_FONT_DIRS = (
    "/usr/share/fonts/truetype/dejavu",
    "C:/Windows/Fonts",
    "/usr/share/fonts/truetype/liberation",
)


def _font(size: int, bold: bool = True) -> ImageFont.FreeTypeFont:
    names = ("DejaVuSans-Bold.ttf", "arialbd.ttf", "LiberationSans-Bold.ttf") if bold else (
        "DejaVuSans.ttf", "arial.ttf", "LiberationSans-Regular.ttf")
    for folder in _FONT_DIRS:
        for name in names:
            path = Path(folder) / name
            if path.is_file():
                return ImageFont.truetype(str(path), size)
    raise RuntimeError("не найден шрифт с кириллицей для карточки")


def _wrap(draw: ImageDraw.ImageDraw, text: str, font, width: int) -> list[str]:
    lines, line = [], ""
    for word in text.split():
        candidate = f"{line} {word}".strip()
        if draw.textlength(candidate, font=font) <= width or not line:
            line = candidate
        else:
            lines.append(line)
            line = word
    if line:
        lines.append(line)
    return lines


def render_text_card(big: str, small: str, path: str | Path, kicker: str = "") -> Path:
    """Нарисовать квадратную карточку и вернуть путь к PNG."""
    image = Image.new("RGB", (SIZE, SIZE), BACKGROUND)
    draw = ImageDraw.Draw(image)
    margin = 90
    width = SIZE - 2 * margin

    # Крупная цифра ужимается, пока не влезет в ширину.
    size = 300
    font = _font(size)
    while draw.textlength(big, font=font) > width and size > 80:
        size -= 10
        font = _font(size)
    box = draw.textbbox((0, 0), big, font=font)
    top = 250
    draw.text((margin, top - box[1]), big, font=font, fill=ACCENT)
    y = top + (box[3] - box[1]) + 70

    draw.rectangle((margin, y, margin + 120, y + 8), fill=ACCENT)
    y += 60
    body = _font(52, bold=False)
    for line in _wrap(draw, small, body, width):
        draw.text((margin, y), line, font=body, fill=TEXT)
        y += 72

    if kicker:
        draw.text((margin, 110), kicker.upper(), font=_font(36), fill=MUTED)
    draw.text((margin, SIZE - 120), "БытТехОпт · климатическая техника",
              font=_font(34, bold=False), fill=MUTED)

    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    image.save(out, "PNG")
    return out
