"""Образцы нового стиля товарной карточки (черновик для согласования, в репозиторий не входит)."""
import sys
from io import BytesIO
from pathlib import Path

import httpx
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

sys.path.insert(0, r"C:\Users\TLT-1\Documents\Codex\1st_content_TG_VK_YT_INSTA_autoload\src")
from content_factory.storefront.catalog_snapshot import load_snapshot  # noqa: E402

S = Path(r"C:/Users/TLT-1/AppData/Local/Temp/claude/C--Users-TLT-1-Documents-Codex/194d8ef0-1fcc-43f3-b66b-118d58117165/scratchpad")
FONT_B = r"C:\Windows\Fonts\segoeuib.ttf"
FONT_R = r"C:\Windows\Fonts\segoeui.ttf"
FONT_SB = r"C:\Windows\Fonts\seguisb.ttf"
NAVY = (20, 38, 63)
GRAY = (104, 118, 135)
ACCENT = (232, 101, 38)
GREEN = (36, 160, 96)
SIZE = 1080


def font(path, size):
    return ImageFont.truetype(path, size)


def load_product(url):
    data = httpx.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=40, follow_redirects=True).content
    src = Image.open(BytesIO(data))
    rgba = src.convert("RGBA")
    flat = Image.new("RGB", rgba.size, (255, 255, 255))
    flat.paste(rgba, mask=rgba.split()[3])
    box = flat.convert("L").point(lambda v: 255 if v < 244 else 0).getbbox()
    if box:
        m = int(0.02 * max(flat.size))
        flat = flat.crop((max(0, box[0] - m), max(0, box[1] - m),
                          min(flat.width, box[2] + m), min(flat.height, box[3] + m)))
    return flat


def gradient():
    top, bottom = (246, 248, 251), (231, 237, 243)
    column = Image.new("RGB", (1, SIZE))
    for y in range(SIZE):
        t = y / (SIZE - 1)
        column.putpixel((0, y), tuple(round(top[i] + (bottom[i] - top[i]) * t) for i in range(3)))
    return column.resize((SIZE, SIZE))


def rounded(draw, box, radius, fill, outline=None, width=2):
    draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=width)


def fit(img, max_w, max_h, max_scale=3.0):
    scale = min(max_w / img.width, max_h / img.height, max_scale)
    return img.resize((max(1, round(img.width * scale)), max(1, round(img.height * scale))),
                      Image.Resampling.LANCZOS)


def render(photo_url, brand, chips, price, out):
    product = load_product(photo_url)
    portrait = product.width / product.height < 0.85
    bg = gradient()

    # зона товара
    if portrait:
        area = (70, 130, 650, 810)
    else:
        area = (90, 130, 990, 610)
    prod = fit(product, area[2] - area[0], area[3] - area[1])
    px = area[0] + (area[2] - area[0] - prod.width) // 2
    py = area[1] + (area[3] - area[1] - prod.height) // 2

    # мягкая тень под товаром
    shadow = Image.new("L", (SIZE, SIZE), 0)
    sd = ImageDraw.Draw(shadow)
    sw = int(prod.width * 0.8)
    sd.ellipse((px + (prod.width - sw) // 2, py + prod.height - 14,
                px + (prod.width + sw) // 2, py + prod.height + 26), fill=95)
    shadow = shadow.filter(ImageFilter.GaussianBlur(16))
    bg = Image.composite(Image.new("RGB", (SIZE, SIZE), (150, 162, 176)), bg, shadow)

    # товар «вплавлен» в подложку: белый фон снимка исчезает
    layer = Image.new("RGB", (SIZE, SIZE), (255, 255, 255))
    layer.paste(prod, (px, py))
    card = ImageChops.multiply(bg, layer)
    d = ImageDraw.Draw(card)

    # бренд
    label = brand.upper()
    f = font(FONT_SB, 30)
    w = d.textlength(label, font=f)
    rounded(d, (56, 52, 56 + w + 52, 52 + 62), 31, NAVY)
    d.text((56 + 26, 52 + 14), label, font=f, fill=(255, 255, 255))

    # плашки с цифрами
    def fit_font(text, path, size, max_width, minimum=20):
        while size > minimum and d.textlength(text, font=font(path, size)) > max_width:
            size -= 2
        return font(path, size)

    def chip(x, y, cw, ch, value, caption):
        rounded(d, (x, y, x + cw, y + ch), 26, (255, 255, 255), outline=(222, 229, 236))
        inner = cw - 48
        d.text((x + 24, y + 14), value, font=fit_font(value, FONT_B, 56, inner), fill=NAVY)
        d.text((x + 24, y + ch - 52), caption, font=fit_font(caption, FONT_R, 27, inner), fill=GRAY)

    if portrait:
        y = 175
        for value, caption in chips[:3]:
            chip(690, y, 330, 168, value, caption)
            y += 196
    else:
        x = 56
        cw = (SIZE - 2 * 56 - 2 * 24) // 3
        for value, caption in chips[:3]:
            chip(x, 650, cw, 150, value, caption)
            x += cw + 24

    # плашка цены
    rounded(d, (56, 846, SIZE - 56, 1024), 36, (255, 255, 255), outline=(222, 229, 236))
    d.text((92, 858), price, font=font(FONT_B, 88), fill=NAVY)
    d.ellipse((96, 984, 114, 1002), fill=GREEN)
    d.text((126, 972), "в наличии · Симферополь", font=font(FONT_R, 30), fill=GRAY)
    site = "splithome.ru"
    f2 = font(FONT_B, 40)
    d.text((SIZE - 92 - d.textlength(site, font=f2), 872), site, font=f2, fill=ACCENT)
    tail = "доставка по Крыму"
    f3 = font(FONT_R, 30)
    d.text((SIZE - 92 - d.textlength(tail, font=f3), 972), tail, font=f3, fill=GRAY)
    card.save(out, "JPEG", quality=92)


SAMPLES = [
    ("rusklimat:NS-1788060", "Royal Thermo", "9 950 ₽",
     [("688 Вт", "теплоотдача"), ("25 бар", "рабочее давление"), ("25 лет", "гарантия")],
     "rusklimat_NS-1788060"),
    ("breeze:NS-1344807", "Royal Clima", "2 590 ₽",
     [("300 мл/ч", "увлажнение в час"), ("3,5 л", "объём бака"), ("11 ч", "без долива воды")],
     "breeze_NS-1344807"),
    ("rusklimat:NS-1588904", "Royal Thermo", "3 300 ₽",
     [("5,5 кВт", "мощность"), ("2–3,7 л/мин", "производительность"), ("до 60 °C", "нагрев воды")],
     "rusklimat_NS-1588904"),
]

if __name__ == "__main__":
    items = {i.id: i for i in load_snapshot(S / "catalog-snapshot.json")}
    new_paths = []
    for oid, brand, price, chips, stem in SAMPLES:
        out = S / f"new_{stem}.jpg"
        render(items[oid].picture, brand, chips, price, out)
        new_paths.append((S / f"{stem}.jpg", out))
        print("готово:", out.name)
    sheet = Image.new("RGB", (3 * 540 + 4 * 20, 2 * 540 + 3 * 20), (255, 255, 255))
    for i, (old, new) in enumerate(new_paths):
        x = 20 + i * (540 + 20)
        sheet.paste(Image.open(old).convert("RGB").resize((540, 540)), (x, 20))
        sheet.paste(Image.open(new).convert("RGB").resize((540, 540)), (x, 20 + 540 + 20))
    sheet.save(S / "style_compare.jpg", quality=90)
    row = Image.new("RGB", (3 * 700 + 4 * 16, 700 + 32), (255, 255, 255))
    for i, (_old, new) in enumerate(new_paths):
        row.paste(Image.open(new).convert("RGB").resize((700, 700)), (16 + i * 716, 16))
    row.save(S / "style_new_row.jpg", quality=92)
    print("лист сравнения: style_compare.jpg")
