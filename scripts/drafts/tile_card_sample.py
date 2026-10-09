"""Светлая товарная карточка в стиле плашек приложения владельца (градиентные «стеклянные» плитки)."""
import sys
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter

sys.path.insert(0, str(Path(__file__).parent))
from card_style_sample import FONT_B, FONT_R, FONT_SB, S, font, load_product  # noqa: E402
from content_factory.storefront.catalog_snapshot import load_snapshot  # noqa: E402

SIZE = 1080
BG = (243, 246, 250)
TEAL, BLUE = (20, 128, 110), (43, 108, 176)
# цвета плиток как в приложении: акцент, цвет числа, цвет рамки, оттенок заливки
TILES = [
    ("teal", (20, 128, 110), (168, 214, 206), (226, 243, 240)),
    ("amber", (140, 92, 24), (222, 196, 160), (248, 238, 226)),
    ("blue", (43, 108, 176), (176, 200, 232), (228, 238, 250)),
    ("purple", (110, 76, 190), (205, 192, 238), (238, 232, 250)),
]


def diag_gradient(w, h, c1, c2):
    """Диагональный градиент c1 → c2 (слева-сверху → справа-снизу)."""
    vertical = Image.linear_gradient("L").resize((w, h), Image.Resampling.BILINEAR)
    horizontal = Image.linear_gradient("L").rotate(90, expand=True).resize((w, h), Image.Resampling.BILINEAR)
    t = ImageChops.add(vertical, horizontal, scale=2)          # (x/w + y/h) / 2
    return Image.composite(Image.new("RGB", (w, h), c2), Image.new("RGB", (w, h), c1), t)


def rounded_mask(w, h, r):
    m = Image.new("L", (w * 2, h * 2), 0)
    ImageDraw.Draw(m).rounded_rectangle((0, 0, w * 2 - 1, h * 2 - 1), radius=r * 2, fill=255)
    return m.resize((w, h), Image.Resampling.LANCZOS)


def shadow(canvas, box, r, alpha=30, blur=18, dy=10):
    x0, y0, x1, y1 = box
    layer = Image.new("L", canvas.size, 0)
    ImageDraw.Draw(layer).rounded_rectangle((x0, y0 + dy, x1, y1 + dy), radius=r, fill=alpha)
    layer = layer.filter(ImageFilter.GaussianBlur(blur))
    canvas.paste(Image.new("RGB", canvas.size, (40, 70, 110)), (0, 0), layer)


def fit_font(draw, text, path, size, max_width, minimum=20):
    while size > minimum and draw.textlength(text, font=font(path, size)) > max_width:
        size -= 2
    return font(path, size)


def tile(canvas, box, tint, border, r=34):
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    shadow(canvas, box, r)
    fill = diag_gradient(w, h, tint, (255, 255, 255))
    canvas.paste(fill, (x0, y0), rounded_mask(w, h, r))
    ImageDraw.Draw(canvas).rounded_rectangle(box, radius=r, outline=border, width=3)


def hero(canvas, box, r=46):
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    shadow(canvas, box, r, alpha=45, blur=22, dy=12)
    canvas.paste(diag_gradient(w, h, TEAL, BLUE), (x0, y0), rounded_mask(w, h, r))


def render(photo_url, kicker, title, subtitle, tiles, price, out):
    canvas = Image.new("RGB", (SIZE, SIZE), BG)
    d = ImageDraw.Draw(canvas)

    # шапка
    hero(canvas, (40, 40, 1040, 238))
    d = ImageDraw.Draw(canvas)
    d.text((84, 70), kicker.upper(), font=fit_font(d, kicker.upper(), FONT_SB, 26, 900), fill=(205, 232, 236))
    d.text((84, 104), title, font=fit_font(d, title, FONT_B, 58, 900), fill=(255, 255, 255))
    d.text((84, 176), subtitle, font=fit_font(d, subtitle, FONT_R, 30, 900), fill=(224, 238, 244))

    # плитка с товаром
    box = (40, 266, 604, 822)
    tile(canvas, box, (236, 242, 248), (203, 216, 232), r=40)
    product = load_product(photo_url)
    w, h = box[2] - box[0], box[3] - box[1]
    scale = min((w - 110) / product.width, (h - 130) / product.height, 3.0)
    product = product.resize((round(product.width * scale), round(product.height * scale)), Image.Resampling.LANCZOS)
    px = box[0] + (w - product.width) // 2
    py = box[1] + (h - product.height) // 2
    sh = Image.new("L", canvas.size, 0)
    sw = int(product.width * .78)
    ImageDraw.Draw(sh).ellipse((px + (product.width - sw) // 2, py + product.height - 12,
                                px + (product.width + sw) // 2, py + product.height + 24), fill=85)
    sh = sh.filter(ImageFilter.GaussianBlur(14))
    canvas.paste(Image.new("RGB", canvas.size, (120, 134, 154)), (0, 0), sh.crop((0, 0, *canvas.size)))
    layer = Image.new("RGB", canvas.size, (255, 255, 255))
    layer.paste(product, (px, py))
    mask = Image.new("L", canvas.size, 0)
    mask.paste(rounded_mask(w, h, 40), (box[0], box[1]))
    canvas.paste(ImageChops.multiply(canvas, layer), (0, 0), mask)

    # плитки с цифрами
    n = min(4, len(tiles))
    gap = 14
    th = (822 - 266 - gap * (n - 1)) // n
    y = 266
    for i, (value, label) in enumerate(tiles[:n]):
        _, accent, border, tint = TILES[i]
        tile(canvas, (624, y, 1040, y + th), tint, border)
        d = ImageDraw.Draw(canvas)
        d.ellipse((652, y + 24, 670, y + 42), fill=accent)
        d.text((682, y + 14), label, font=fit_font(d, label, FONT_R, 27, 330), fill=(86, 100, 118))
        d.text((650, y + th - 78), value, font=fit_font(d, value, FONT_B, 54, 360), fill=accent)
        y += th + gap

    # плашка цены
    hero(canvas, (40, 848, 1040, 1040))
    d = ImageDraw.Draw(canvas)
    d.text((84, 872), "Цена", font=font(FONT_R, 28), fill=(214, 234, 240))
    d.text((84, 904), price, font=font(FONT_B, 86), fill=(255, 255, 255))
    right = 996
    t1 = "в наличии · Симферополь"
    f1 = font(FONT_SB, 30)
    w1 = d.textlength(t1, font=f1)
    d.ellipse((right - w1 - 36, 937, right - w1 - 16, 957), fill=(132, 232, 170))
    d.text((right - w1, 924), t1, font=f1, fill=(255, 255, 255))
    t2 = "доставка по Крыму"
    f2 = font(FONT_R, 28)
    d.text((right - d.textlength(t2, font=f2), 974), t2, font=f2, fill=(224, 238, 244))
    canvas.save(out, "JPEG", quality=93)


SAMPLES = [
    ("rusklimat:NS-1145974", "Electrolux · тёплые полы", "Нагревательный мат EEM 2-150-4",
     "С терморегулятором в комплекте", "11 575 ₽",
     [("150 Вт/м²", "удельная мощность"), ("до 4 м²", "площадь обогрева"),
      ("IP68", "защита от влаги"), ("20 лет", "гарантия")], "tile_floor"),
    ("rusklimat:NS-1407892", "Ballu · сплит-системы", "Ballu Olympio Edge BSO-12HN8",
     "Охлаждение и обогрев, 12 000 BTU", "39 090 ₽",
     [("12 000 BTU", "мощность охлаждения"), ("до 35 м²", "площадь помещения"),
      ("26 дБ", "шум внутреннего блока"), ("5 лет", "гарантия на компрессор")], "tile_ac"),
    ("rusklimat:NS-1237353", "Electrolux · водонагреватели", "Водонагреватель EWH 50 SmartInverter",
     "Накопительный, управление по Wi-Fi", "30 470 ₽",
     [("50 л", "номинальный объём"), ("до 75 °C", "температура воды"),
      ("Wi-Fi", "управление со смартфона"), ("8 лет", "гарантия на бак")], "tile_heater"),
]

if __name__ == "__main__":
    items = {i.id: i for i in load_snapshot(S / "catalog-snapshot.json")}
    for oid, kicker, title, sub, price, tiles, stem in SAMPLES:
        render(items[oid].picture, kicker, title, sub, tiles, price, S / f"{stem}.jpg")
        print("готово:", stem)
