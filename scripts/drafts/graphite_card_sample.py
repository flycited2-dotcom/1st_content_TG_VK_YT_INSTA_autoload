"""Макет графитовой карточки (стиль владельца) с товаром на светлой «студийной» подложке."""
import sys
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

sys.path.insert(0, str(Path(__file__).parent))
from card_style_sample import FONT_B, FONT_R, FONT_SB, S, load_product  # noqa: E402
from content_factory.storefront.catalog_snapshot import load_snapshot  # noqa: E402

SIZE = 1080
GOLD = (214, 172, 96)
GOLD_LIGHT = (242, 208, 146)
WHITE = (246, 246, 247)
GRAY = (166, 169, 176)
GLASS = (255, 255, 255, 16)
GLASS_LINE = (214, 172, 96, 120)
GREEN = (84, 196, 128)


def font(path, size):
    return ImageFont.truetype(path, size)


def fit_font(draw, text, path, size, max_width, minimum=18):
    while size > minimum and draw.textlength(text, font=font(path, size)) > max_width:
        size -= 2
    return font(path, size)


# ── значки: рисуются в 4-кратном размере и уменьшаются ──
def icon(kind, size=56, color=GOLD):
    k = 4
    n = size * k
    im = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    w = max(2, n // 14)
    p = lambda x, y: (x * n, y * n)  # noqa: E731
    if kind == "bolt":
        d.polygon([p(.58, .06), p(.22, .56), p(.46, .56), p(.38, .94), p(.78, .40), p(.54, .40)], fill=color)
    elif kind == "drop":
        d.polygon([p(.5, .06), p(.2, .55), p(.8, .55)], fill=color)
        d.ellipse([p(.2, .38), p(.8, .92)], fill=color)
    elif kind == "gauge":
        d.arc([p(.1, .14), p(.9, .94)], 180, 360, fill=color, width=w)
        d.line([p(.5, .54), p(.74, .30)], fill=color, width=w)
        d.ellipse([p(.43, .47), p(.57, .61)], fill=color)
        d.line([p(.1, .54), p(.9, .54)], fill=color, width=w)
    elif kind == "shield":
        d.polygon([p(.5, .06), p(.86, .2), p(.82, .58), p(.5, .94), p(.18, .58), p(.14, .2)], outline=color, width=w)
        d.line([p(.34, .5), p(.46, .62), p(.68, .36)], fill=color, width=w)
    elif kind == "clock":
        d.ellipse([p(.08, .08), p(.92, .92)], outline=color, width=w)
        d.line([p(.5, .26), p(.5, .52), p(.7, .64)], fill=color, width=w)
    elif kind == "layers":
        for i in range(4):
            x0 = .16 + i * .2
            d.rounded_rectangle([p(x0, .12), p(x0 + .13, .9)], radius=n // 20, fill=color)
    elif kind == "pin":
        d.ellipse([p(.22, .08), p(.78, .64)], outline=color, width=w)
        d.polygon([p(.3, .56), p(.7, .56), p(.5, .94)], fill=color)
        d.ellipse([p(.4, .26), p(.6, .46)], fill=color)
    elif kind == "truck":
        d.rectangle([p(.06, .28), p(.58, .7)], outline=color, width=w)
        d.polygon([p(.58, .4), p(.8, .4), p(.94, .56), p(.94, .7), p(.58, .7)], outline=color, width=w)
        for cx in (.26, .76):
            d.ellipse([p(cx - .09, .62), p(cx + .09, .8)], fill=color, outline=(20, 21, 24), width=2)
    elif kind == "tag":
        d.polygon([p(.1, .12), p(.55, .12), p(.92, .5), p(.52, .92), p(.1, .5)], outline=color, width=w)
        d.ellipse([p(.24, .24), p(.38, .38)], fill=color)
    return im.resize((size, size), Image.Resampling.LANCZOS)


def background():
    top, bottom = (31, 33, 38), (13, 14, 17)
    col = Image.new("RGB", (1, SIZE))
    for y in range(SIZE):
        t = y / (SIZE - 1)
        col.putpixel((0, y), tuple(round(top[i] + (bottom[i] - top[i]) * t) for i in range(3)))
    bg = col.resize((SIZE, SIZE))
    glow = Image.new("L", (SIZE, SIZE), 0)
    ImageDraw.Draw(glow).ellipse((60, 260, 700, 900), fill=70)
    glow = glow.filter(ImageFilter.GaussianBlur(140))
    return Image.composite(Image.new("RGB", (SIZE, SIZE), (122, 84, 40)), bg, glow)


def glass(canvas, box, radius=28, line=True):
    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    d.rounded_rectangle(box, radius=radius, fill=GLASS, outline=GLASS_LINE if line else None, width=2)
    canvas.alpha_composite(layer)


def stage(photo_url, box):
    """Светлая подложка с мягким светом, на ней товар «вплавлен» без вырезки."""
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    base = Image.new("RGB", (w, h), (214, 218, 225))
    spot = Image.new("L", (w, h), 0)
    ImageDraw.Draw(spot).ellipse((int(w * .05), int(h * .02), int(w * .95), int(h * .9)), fill=255)
    spot = spot.filter(ImageFilter.GaussianBlur(70))
    base = Image.composite(Image.new("RGB", (w, h), (251, 251, 253)), base, spot)
    product = load_product(photo_url)
    scale = min((w - 90) / product.width, (h - 130) / product.height, 3.0)
    product = product.resize((round(product.width * scale), round(product.height * scale)), Image.Resampling.LANCZOS)
    px, py = (w - product.width) // 2, (h - product.height) // 2 - 10
    shadow = Image.new("L", (w, h), 0)
    sw = int(product.width * .8)
    ImageDraw.Draw(shadow).ellipse((px + (product.width - sw) // 2, py + product.height - 12,
                                    px + (product.width + sw) // 2, py + product.height + 26), fill=90)
    shadow = shadow.filter(ImageFilter.GaussianBlur(14))
    base = Image.composite(Image.new("RGB", (w, h), (120, 128, 140)), base, shadow)
    layer = Image.new("RGB", (w, h), (255, 255, 255))
    layer.paste(product, (px, py))
    base = ImageChops.multiply(base, layer)
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, w - 1, h - 1), radius=34, fill=255)
    return base, mask


def render(photo_url, brand, title, subtitle, specs, rows, out):
    canvas = background().convert("RGBA")
    d = ImageDraw.Draw(canvas)

    # шапка: бренд | заголовок
    f_brand = fit_font(d, brand.upper(), FONT_B, 58, 340)
    d.text((44, 74), brand.upper(), font=f_brand, fill=WHITE)
    d.line([(404, 48), (404, 178)], fill=(255, 255, 255, 60), width=2)
    d.text((434, 46), title, font=fit_font(d, title, FONT_B, 50, 606), fill=WHITE)
    d.text((434, 112), subtitle, font=fit_font(d, subtitle, FONT_SB, 38, 606), fill=GOLD_LIGHT)

    # полоса главных параметров
    glass(canvas, (44, 206, SIZE - 44, 292), radius=30)
    d = ImageDraw.Draw(canvas)
    cell = (SIZE - 88) / 3
    for i, (kind, value) in enumerate(specs[:3]):
        cx = 44 + i * cell
        canvas.alpha_composite(icon(kind, 46), (int(cx + 34), 226))
        d.text((cx + 96, 228), value, font=fit_font(d, value, FONT_B, 38, cell - 120), fill=WHITE)
        if i:
            d.line([(cx, 224), (cx, 274)], fill=(255, 255, 255, 50), width=2)

    # сцена с товаром
    box = (44, 316, 650, 904)
    st, mask = stage(photo_url, box)
    canvas.paste(st.convert("RGBA"), (box[0], box[1]), mask)
    d = ImageDraw.Draw(canvas)

    # панель преимуществ
    d.text((684, 322), "Преимущества", font=font(FONT_B, 36), fill=WHITE)
    y = 380
    row_h = min(124, (904 - 380 - 10 * (len(rows) - 1)) // max(1, len(rows)))
    for kind, label, value in rows[:5]:
        glass(canvas, (680, y, SIZE - 44, y + row_h), radius=26)
        d = ImageDraw.Draw(canvas)
        d.ellipse((698, y + (row_h - 62) // 2, 760, y + (row_h - 62) // 2 + 62), outline=GOLD, width=3)
        canvas.alpha_composite(icon(kind, 34), (712, y + (row_h - 34) // 2))
        d.text((780, y + 16), label, font=fit_font(d, label, FONT_R, 25, 250), fill=GRAY)
        d.text((780, y + 16 + 32), value, font=fit_font(d, value, FONT_B, 33, 250), fill=WHITE)
        y += row_h + 10

    # нижняя полоса
    glass(canvas, (44, 934, SIZE - 44, 1032), radius=30)
    d = ImageDraw.Draw(canvas)
    canvas.alpha_composite(icon("pin", 46), (84, 960))
    d.text((144, 966), "Самовывоз в Симферополе", font=font(FONT_SB, 32), fill=WHITE)
    d.line([(612, 954), (612, 1012)], fill=(255, 255, 255, 50), width=2)
    canvas.alpha_composite(icon("truck", 50), (648, 958))
    d.text((714, 966), "Доставка по Крыму", font=font(FONT_SB, 32), fill=WHITE)
    canvas.convert("RGB").save(out, "JPEG", quality=92)


SAMPLES = [
    ("rusklimat:NS-1788060", "Royal Thermo", "Радиатор биметаллический", "Eclipse 500 · 4 секции",
     [("bolt", "688 Вт"), ("gauge", "до 25 бар"), ("layers", "4 секции")],
     [("layers", "Тип", "биметаллический"), ("bolt", "Теплоотдача", "688 Вт"),
      ("gauge", "Рабочее давление", "до 25 бар"), ("shield", "Гарантия", "25 лет")],
     "graphite_radiator"),
    ("breeze:NS-1344807", "Royal Clima", "Увлажнитель воздуха", "LORETO RUH-LT300/3.5M-BU",
     [("drop", "300 мл/ч"), ("drop", "бак 3,5 л"), ("clock", "11 ч без долива")],
     [("drop", "Увлажнение", "до 300 мл в час"), ("drop", "Объём бака", "3,5 л"),
      ("clock", "Без долива воды", "до 11 часов")],
     "graphite_humidifier"),
]

if __name__ == "__main__":
    items = {i.id: i for i in load_snapshot(S / "catalog-snapshot.json")}
    for oid, brand, title, sub, specs, rows, stem in SAMPLES:
        render(items[oid].picture, brand, title, sub, specs, rows, S / f"{stem}.jpg")
        print("готово:", stem)
