"""Детерминированная карточка: товар не генерируется и не меняет геометрию."""
from __future__ import annotations

import hashlib
import io
import re
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps


@dataclass
class ExactCardSpec:
    brand: str
    product_type: str
    model: str
    metrics: list[str] = field(default_factory=list)
    features: list[str] = field(default_factory=list)


def _font(size: int, bold: bool = False):
    names = (["C:/Windows/Fonts/arialbd.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"]
             if bold else
             ["C:/Windows/Fonts/arial.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"])
    for name in names:
        if Path(name).is_file():
            return ImageFont.truetype(name, size=size)
    return ImageFont.load_default()


def _fit_font(draw, text: str, max_width: int, start: int, *, bold=False, minimum=16):
    for size in range(start, minimum - 1, -2):
        font = _font(size, bold=bold)
        if draw.textbbox((0, 0), text, font=font)[2] <= max_width:
            return font
    return _font(minimum, bold=bold)


def _source_image(value) -> tuple[Image.Image, bytes]:
    if isinstance(value, (bytes, bytearray)):
        raw = bytes(value)
    else:
        raw = Path(value).read_bytes()
    image = Image.open(io.BytesIO(raw)).convert("RGB")
    return image, raw


def compose_exact_product_card(source_image, template_path, output_path, spec: ExactCardSpec) -> dict:
    """Вставить исходное фото без crop/rotate/skew. Разрешено только равномерное масштабирование."""
    template = Image.open(template_path).convert("RGB").resize((1024, 1024), Image.Resampling.LANCZOS)
    source, raw = _source_image(source_image)
    source_size = source.size
    panel = (36, 330, 620, 938)
    max_size = (panel[2] - panel[0] - 18, panel[3] - panel[1] - 18)
    fitted = ImageOps.contain(source, max_size, method=Image.Resampling.LANCZOS)
    x = panel[0] + (panel[2] - panel[0] - fitted.width) // 2
    y = panel[1] + (panel[3] - panel[1] - fitted.height) // 2
    template.paste(fitted, (x, y))

    draw = ImageDraw.Draw(template)
    gold, white, muted = "#e7b968", "#f5f5f2", "#d8d8d3"
    brand = (spec.brand or "").upper()
    draw.text((48, 42), brand, font=_fit_font(draw, brand, 570, 58, bold=True), fill=gold)
    ptype = (spec.product_type or "ТОВАР").upper()
    draw.text((48, 118), ptype, font=_fit_font(draw, ptype, 570, 34, bold=True), fill=white)
    model = (spec.model or "").upper()
    draw.text((48, 170), model, font=_fit_font(draw, model, 570, 44, bold=True), fill=gold)

    metric_y = (168, 309)
    for index, value in enumerate((spec.metrics or [])[:2]):
        font = _fit_font(draw, value, 285, 43, bold=True)
        box = draw.textbbox((0, 0), value, font=font)
        draw.text((828 - (box[2] - box[0]) // 2, metric_y[index]), value, font=font, fill=gold)

    draw.text((670, 444), "Преимущества", font=_font(28, bold=True), fill=gold)
    feature_y = (511, 605, 699, 793, 887)
    for y0, value in zip(feature_y, (spec.features or [])[:5]):
        clean = re.sub(r"^[^A-Za-zА-Яа-я0-9]+", "", value).strip()
        font = _fit_font(draw, clean, 285, 23, minimum=15)
        draw.text((690, y0), clean, font=font, fill=muted)

    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.suffix.lower() in {".jpg", ".jpeg"}:
        template.save(out, quality=96, subsampling=0)
    else:
        template.save(out)
    sx = fitted.width / source_size[0]
    sy = fitted.height / source_size[1]
    # Один пиксель округления неизбежен при raster resize; это не геометрическая деформация.
    aspect_error = abs((fitted.width / fitted.height) - (source_size[0] / source_size[1]))
    return {
        "source_sha256": hashlib.sha256(raw).hexdigest(),
        "source_size": source_size,
        "placed_size": fitted.size,
        "scale_x": sx,
        "scale_y": sy,
        "geometry_preserved": aspect_error <= (1 / max(fitted.size)),
        "aspect_ratio_error": aspect_error,
        "rotation_degrees": 0,
        "perspective_transform": False,
    }


def card_spec_for_offer(offer) -> ExactCardSpec:
    title = offer.model or ""
    attrs = offer.attrs or {}
    model = str(attrs.get("Артикул") or "").strip()
    if not model:
        tokens = re.findall(r"\b[A-ZА-Я]{2,}[A-ZА-Я0-9-]*\d[A-ZА-Я0-9-]*\b", title.upper())
        model = tokens[0] if tokens else title[:40]
    metrics = []
    for pattern in (r"(\d[\d\s]*(?:[.,]\d+)?\s*к?ВА)", r"(\d+(?:[.,]\d+)?\s*[АВ])"):
        match = re.search(pattern, title, re.I)
        if match:
            metrics.append(re.sub(r"\s+", " ", match.group(1)).upper())
    features = []
    if re.search(r"однофаз", title, re.I):
        features.append("Однофазный")
    voltage = re.search(r"вх\.?\s*:?\s*(\d+\s*[-–]\s*\d+\s*В)", title, re.I)
    if voltage:
        features.append("Вход " + voltage.group(1).replace("-", "–"))
    if re.search(r"настенн", title, re.I):
        features.append("Настенное исполнение")
    if re.search(r"цифров", title, re.I):
        features.append("Цифровая индикация")
    warranty = str(attrs.get("Гарантия") or "").strip()
    if warranty and warranty not in {"0", "0.0"}:
        features.append("Гарантия " + warranty + (" месяцев" if warranty.isdigit() else ""))
    return ExactCardSpec(brand=offer.brand, product_type="Стабилизатор напряжения",
                         model=model, metrics=metrics[:2], features=features[:5])
