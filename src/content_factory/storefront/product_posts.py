"""Товарные посты VK из каталога в наличии: продающий текст, фото, ссылка на карточку.

Текст собирается из данных самого каталога — названия, цены, описания и
характеристик производителя — без LLM и без выдумок: каждая цифра в посте
либо взята из карточки, либо проверена на живой странице сайта перед выбором.
Качество держится на шаблонах и отборе предложений, а не на генерации.
"""
from __future__ import annotations

import glob
import hashlib
import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import httpx

# Группы товаров и то, какие названия категорий каталога в них попадают.
GROUP_BY_CATEGORY = (
    ("ac", ("сплит-систем", "кондиционер")),
    ("vent", ("вентиляцион", "рекуператор")),
    ("air", ("увлажнител", "осушител")),
    ("heater", ("конвектор", "инфракрасн", "тепловент", "тепловые завесы",
                "тепловые пушки", "масляные радиатор")),
    ("radiator", ("радиаторы отопления", "котл")),
    ("water", ("водонагревател", "бойлер")),
    ("floor", ("тёплые полы", "теплые полы", "терморегулятор")),
)

# Вес групп по месяцам: осенью и зимой больше обогрева, весной и летом — кондиционеров.
_WARM = {"ac": 14, "vent": 10, "air": 10, "heater": 26, "radiator": 20, "water": 10, "floor": 10}
_COLD = {"ac": 25, "vent": 10, "air": 7, "heater": 24, "radiator": 14, "water": 10, "floor": 10}
_SPRING = {"ac": 38, "vent": 15, "air": 14, "heater": 6, "radiator": 8, "water": 11, "floor": 8}
_SUMMER = {"ac": 46, "vent": 12, "air": 14, "heater": 3, "radiator": 5, "water": 12, "floor": 8}
MONTH_WEIGHTS = {
    1: _COLD, 2: _COLD, 3: _SPRING, 4: _SPRING, 5: _SPRING, 6: _SUMMER,
    7: _SUMMER, 8: _SUMMER, 9: _WARM, 10: _WARM, 11: _WARM, 12: _COLD,
}

GOLDEN = 0.6180339887498949

_NUM_UNIT = re.compile(
    r"[0-9]+(?:[.,][0-9]+)?\s*(?:дБ|кВт|Вт|л\b|мл|м2|м²|м³|часов|час|°|%|секци|кг|лет|год|BTU)", re.I)
_BAD_SENTENCE = re.compile(r"https?://|www\.|®|™|патент|©|\.ru\b|youtube|гарантийн[а-я]* срок", re.I)
# Предложения про линейку, кабель и настройки — про серию или комплектацию, а не про выгоду.
_SERIES_SENTENCE = re.compile(
    r"линейк|модельн|моделей|модели\b|серии\b|серия\b|типоразмер|кабел|докупа|комплектаци|"
    r"настроен|перезапуска|в комплект|2000|предназначен[а-я]* для работы|"
    r"бренд|компани|продукты |продукци|производств[а-я]* контрол|этапах производства|"
    r"условия эксплуатации|окружающего воздуха|относительной влажности", re.I)

# Промышленные и нестандартные позиции — не для розничной группы.
_NOT_FOR_RETAIL_NAME = re.compile(
    r"канальн|кассетн|колонн|потолочн|антивандал|IP ?54|внутрипольн|промышл|ECO ?[0-9]{3}", re.I)
_NOT_FOR_RETAIL_CATEGORY = re.compile(r"полупромышленн|компактные моноблочные|мульти", re.I)
# Аксессуары и расходники: в «Кондиционерах» нагреватель дренажа за 593 ₽ — не витрина.
_ACCESSORY_NAME = re.compile(
    r"дренаж|кронштейн|пульт|сифон|трубк|крепеж|крепёж|фреон|хладагент|помп[аы]|насос|"
    r"защитн|чехол|рамк[аи]|решетк|решётк|заглушк|переходник|комплект монтажн", re.I)
PRICE_CAP = {"ac": 100_000, "vent": 150_000, "air": 40_000, "heater": 25_000,
             "radiator": 30_000, "water": 70_000, "floor": 40_000}
PRICE_FLOOR = {"ac": 8_000, "vent": 8_000, "air": 1_500, "heater": 1_200,
               "radiator": 2_500, "water": 1_500, "floor": 1_000}
_SKIP_ATTR = re.compile(r"упаковк|масса|гарантийн|сетевой кабель|модель|бренд|серия|ссылка|видео", re.I)

# Потребительские характеристики: (шаблон ключа, что писать, когда значение «да»).
BOOL_FEATURES = (
    (re.compile(r"тепловой насос", re.I), "Работает на обогрев"),
    (re.compile(r"инверторн", re.I), "Инверторная технология"),
    (re.compile(r"wi-?fi", re.I), "Управление со смартфона по Wi-Fi"),
    (re.compile(r"таймер", re.I), "Таймер отключения"),
    (re.compile(r"защита от перегрева", re.I), "Защита от перегрева"),
    (re.compile(r"ручка для перемещения", re.I), "Ручка для переноски"),
    (re.compile(r"самодиагностик", re.I), "Самодиагностика неисправностей"),
)
VALUE_FEATURES = (
    (re.compile(r"минимальная температура обогрева", re.I), "Работает на обогрев до {v}"),
    (re.compile(r"макс\.? температура теплоносителя", re.I), "Теплоноситель до {v}"),
    (re.compile(r"макс\.? площадь обогрева", re.I), "Обогрев до {v}"),
    (re.compile(r"объем воды в радиаторе", re.I), "Объём воды в радиаторе {v}"),
    (re.compile(r"удельная мощность", re.I), "Мощность {v}"),
)

CITY = "в наличии в Симферополе"
PHONE = "+7 978 579-29-95"


@dataclass(frozen=True)
class CatalogItem:
    id: str
    url: str
    price: int
    group: str
    category: str
    picture: str
    name: str
    brand: str
    prose: str
    attrs: dict = field(default_factory=dict, compare=False)


def group_of(category: str) -> str:
    low = category.casefold()
    for group, needles in GROUP_BY_CATEGORY:
        if any(needle in low for needle in needles):
            return group
    return "other"


def retail_ok(category: str, name: str, price: int) -> bool:
    """Годится ли позиция для розничной витрины: известная группа, не промышленная,
    не аксессуар и цена в пределах своей группы. Общая для всех источников каталога."""
    group = group_of(category)
    if group == "other" or price <= 0:
        return False
    if (_NOT_FOR_RETAIL_CATEGORY.search(category) or _NOT_FOR_RETAIL_NAME.search(name)
            or _ACCESSORY_NAME.search(name)):
        return False
    return PRICE_FLOOR[group] <= price <= PRICE_CAP[group]


def _brand(name: str) -> str:
    # Бренд в каталоге обычно идёт среди первых слов названия латиницей или капсом.
    words = name.split()
    for word in words[:4]:
        if word.isupper() or (word[:1].isupper() and word[1:].islower() and word.isascii()):
            return word.casefold()
    return words[0].casefold() if words else ""


def parse_description(desc: str) -> tuple[str, dict]:
    """Разделить описание на прозу производителя и словарь характеристик."""
    head, _, tail = desc.partition("Характеристики:")
    attrs = {}
    for match in re.finditer(r"^•\s*([^:\n]+):\s*(.+)$", tail, re.M):
        attrs[match.group(1).strip()] = match.group(2).strip()
    # В некоторых карточках характеристики лежат прямо в тексте через «;».
    for match in re.finditer(r"([А-ЯЁA-Z][^:;\n]{3,40}):\s*([^;\n]{1,40})(?:;|\.|$)", head):
        attrs.setdefault(match.group(1).strip(), match.group(2).strip())
    return head.strip(), attrs


def load_catalog(directory: str | Path) -> list[CatalogItem]:
    items = []
    for path in sorted(glob.glob(str(Path(directory) / "*.yml"))):
        root = ET.parse(path).getroot()
        categories = {c.get("id"): (c.text or "") for c in root.iter("category")}
        for offer in root.iter("offer"):
            def get(tag):
                return (offer.findtext(tag) or "").strip()
            try:
                price = int(float(get("price") or 0))
            except ValueError:
                continue
            category = categories.get(get("categoryId"), "")
            group = group_of(category)
            name = get("name")
            if not (get("picture") and get("url") and retail_ok(category, name, price)):
                continue
            prose, attrs = parse_description(get("description"))
            items.append(CatalogItem(
                id=str(offer.get("id")), url=get("url"), price=price, group=group,
                category=category, picture=get("picture"), name=name,
                brand=_brand(name), prose=prose, attrs=attrs,
            ))
    return items


def money(value: int) -> str:
    return f"{int(value):,}".replace(",", " ") + " ₽"


# ── проза производителя → продающие предложения ─────────────────────────────

def benefit_sentences(item: CatalogItem, limit: int = 2) -> list[str]:
    title = item.name.casefold()
    scored = []
    seen = set()
    for line in item.prose.splitlines():
        line = re.sub(r"\s+", " ", line).strip()
        if not line or line.casefold().startswith(title[:30]):
            continue
        for sentence in re.split(r"(?<=[.!?])\s+", line):
            sentence = sentence.strip()
            if not 35 <= len(sentence) <= 190 or _BAD_SENTENCE.search(sentence) \
                    or _SERIES_SENTENCE.search(sentence):
                continue
            if not sentence[0].isupper() or sentence[-1] not in ".!?":
                continue
            if sentence.casefold() in seen or sentence.casefold().startswith(("описание", "характеристики")):
                continue
            seen.add(sentence.casefold())
            scored.append((bool(_NUM_UNIT.search(sentence)), len(scored), sentence))
    # Продаёт конкретика с цифрой и единицей. Общие слова («элегантный дизайн»)
    # берём лишь как единственное предложение, когда конкретики в описании нет.
    concrete = [row for row in scored if row[0]][:limit]
    chosen = concrete or [row for row in scored if not row[0]][:1]
    return [row[2] for row in sorted(chosen, key=lambda row: row[1])]


def feature_bullets(item: CatalogItem, limit: int = 4) -> list[str]:
    bullets = []
    for key, value in item.attrs.items():
        is_value_key = any(p.search(key) for p, _ in VALUE_FEATURES)
        if _SKIP_ATTR.search(key) and not is_value_key:
            continue
        low = value.casefold().strip(" .")
        for pattern, text in BOOL_FEATURES:
            if pattern.search(key) and low in {"да", "есть", "имеется"}:
                bullets.append(text)
        for pattern, template in VALUE_FEATURES:
            if pattern.search(key) and low not in {"нет", "-", ""}:
                bullets.append(template.format(v=value.strip()))
    return list(dict.fromkeys(bullets))[:limit]


# ── числа для крючков ───────────────────────────────────────────────────────

def _first(pattern: str, *texts: str) -> str:
    for text in texts:
        match = re.search(pattern, text, re.I)
        if match:
            return match.group(1).replace(",", ".")
    return ""


def _yes(item: CatalogItem, key_part: str) -> str:
    for key, value in item.attrs.items():
        if key_part in key.casefold() and value.casefold().strip(" .") in {"да", "есть", "имеется"}:
            return "да"
    return ""


def _single_model_area(item: CatalogItem) -> str:
    """Площадь только из предложения про эту модель: диапазон линейки — не её площадь."""
    if item.group == "ac":
        return ""
    for sentence in re.split(r"(?<=[.!?])\s+", item.prose):
        if _SERIES_SENTENCE.search(sentence):
            continue
        value = _first(r"до\s+([0-9]{1,3})\s*(?:м2|м²|кв\.?\s?м|квадратн)", sentence)
        if value:
            return value
    return ""


def facts_for_hook(item: CatalogItem) -> dict:
    blob = f"{item.name}\n{item.prose}"
    return {
        "area": _single_model_area(item),
        "heatpump": _yes(item, "тепловой насос"),
        "inverter": _yes(item, "инверторн"),
        "liters": _first(r"([0-9]{2,3})\s*л\b", item.name),
        "kw": _first(r"([0-9](?:[.,][0-9]+)?)\s*кВт", item.name, item.prose),
        "sections": _first(r"([0-9]{1,2})\s*секц", item.name),
        "sections_text": "",
        "noise": _first(r"от\s+([0-9]{2})\s*дБ", item.prose),
    }


HOOKS = {
    "ac": (
        ("heatpump", "Кондиционер, который не уходит в отпуск на зиму: у этой модели есть режим обогрева. Вот она в наличии."),
        ("heatpump", "Один прибор и на жару, и на осенние вечера: у этого кондиционера есть режим обогрева."),
        ("inverter", "Инверторный кондиционер плавно меняет обороты и держит температуру без рывков. Вот модель, которая есть в наличии."),
        ("inverter", "Хотите, чтобы температура не «пилила» вверх-вниз? Смотрите на инвертор. Вот модель в наличии."),
        ("", "Выбираете кондиционер? Вот модель в наличии, с честной ценой и монтажом по всему Крыму."),
        ("", "Кондиционер — это не роскошь, а комфорт на каждый день. Вот хороший вариант в наличии."),
    ),
    "vent": (
        ("area", "Душно, а форточку не открыть: шум и пыль с улицы. Приточная установка для помещения до {area} м² решает это без окна нараспашку."),
        ("", "Окна закрыты, а воздух свежий? Так работает приточная вентиляция. Вот модель в наличии."),
        ("", "Свежий воздух без сквозняков и уличного шума. Вот вентиляционная установка, которую можно привезти уже сейчас."),
    ),
    "air": (
        ("", "Сухой воздух зимой — это пересохшее горло и сон хуже обычного. Вот прибор, который это исправляет."),
        ("", "Влажность в доме — тихий фактор комфорта и здоровья. Присмотритесь к этому прибору, он в наличии."),
        ("", "Воздух в квартире бывает слишком сухим или слишком влажным. Вот аккуратный способ его выровнять."),
    ),
    "heater": (
        ("kw", "Холодные вечера не ждут. Обогреватель на {kw} кВт — тепло в комнате без ожидания отопления."),
        ("", "Холодно, а отопление ещё не включили? Вот обогреватель, который можно поставить сегодня."),
        ("", "Нужно быстро согреть комнату, гараж или дачу? Вот вариант в наличии, цена открытая."),
        ("", "Тепло там, где оно нужно: этот обогреватель включается и работает, без ремонта и монтажа."),
        ("", "Зябко по утрам и вечерам? Иногда проще согреть одну комнату, чем ждать тепла во всём доме."),
        ("", "Дача, гараж, мастерская или детская: вот обогреватель, который есть в наличии прямо сейчас."),
    ),
    "heater:curtain": (
        ("", "Каждый раз, когда открывается дверь, тепло уходит на улицу. Тепловая завеса закрывает проём воздушной стеной."),
        ("", "Входная группа магазина, склада или мастерской — самое холодное место. Вот тепловая завеса в наличии."),
    ),
    "heater:gun": (
        ("", "Быстро прогреть гараж, склад или стройку? Для этого и существует тепловая пушка. Вот модель в наличии."),
        ("", "Тепловая пушка — когда тепло нужно быстро и сразу. Вот вариант в наличии, цена открытая."),
    ),
    "radiator": (
        ("sections", "Радиатор на {sections_text}: замена старой батареи без лишней возни. Модель в наличии."),
        ("", "Меняете отопление или достраиваете дом? Вот радиатор, который есть в наличии и не придётся ждать поставку."),
        ("", "Тёплая зима начинается с хорошего радиатора. Вот модель в наличии с ценой без сюрпризов."),
        ("", "Батарея — это на годы, поэтому выбирать её стоит спокойно. Вот модель, которую можно посмотреть и взять."),
        ("", "Холодная комната у окна? Возможно, пора менять радиатор. Вот вариант в наличии."),
    ),
    "water": (
        ("liters", "Водонагреватель на {liters} литров: горячая вода по расписанию семьи, а не по расписанию коммунальщиков."),
        ("", "Горячая вода без отключений и ожидания. Вот водонагреватель, который есть в наличии."),
        ("", "Горячая вода нужна всегда, а не по графику. Вот модель, которую можно взять уже на этой неделе."),
        ("", "Утренний душ не должен зависеть от графика отключений. Вот водонагреватель в наличии."),
    ),
    "floor": (
        ("", "Тёплый пол — это когда утром не хочется надевать носки. Вот комплект, который есть в наличии."),
        ("", "Тёплый пол решает вечную проблему холодного пола в ванной и на кухне. Вот вариант в наличии."),
    ),
}

_ASK = (
    "Подскажем, подойдёт ли вам именно эта модель, — напишите в сообщения сообщества.",
    "Есть вопросы по выбору? Ответим в сообщениях сообщества.",
)
# Монтаж упоминаем только там, где он есть: увлажнителю установка не нужна.
CTAS = {
    "ac": ("Привезём и установим по всему Крыму.", "Доставка и монтаж по Крыму, консультация бесплатная.") + _ASK,
    "vent": ("Привезём и установим по всему Крыму.", "Доставка и монтаж по Крыму, консультация бесплатная.") + _ASK,
    "radiator": ("Доставим по Крыму, поможем с подбором секций.",) + _ASK,
    "water": ("Доставим по Крыму.",) + _ASK,
    "floor": ("Доставим по Крыму и подскажем с монтажом.",) + _ASK,
    "heater": ("Доставим по Крыму.",) + _ASK,
    "air": ("Доставим по Крыму.",) + _ASK,
}


def _hook_key(item: CatalogItem) -> str:
    """Обогреватели бывают разные: у завесы и пушки свои сценарии, не «дача и гараж»."""
    low = item.category.casefold()
    if item.group == "heater" and "завес" in low:
        return "heater:curtain"
    if item.group == "heater" and "пушк" in low:
        return "heater:gun"
    return item.group


def _sections_word(count: str) -> str:
    number = int(count)
    if 11 <= number % 100 <= 14:
        return "секций"
    return {1: "секция", 2: "секции", 3: "секции", 4: "секции"}.get(number % 10, "секций")


def _variant(item: CatalogItem, size: int, salt: str = "") -> int:
    return int(hashlib.sha1((item.id + salt).encode()).hexdigest(), 16) % size


def write_post(item: CatalogItem, price: int | None = None) -> str:
    """Собрать текст товарного поста; ссылка на карточку добавляется отдельно."""
    price = price or item.price
    facts = facts_for_hook(item)
    if facts["sections"]:
        facts["sections_text"] = f"{facts['sections']} {_sections_word(facts['sections'])}"
    hooks = HOOKS.get(_hook_key(item), HOOKS.get(item.group, ()))
    options = [text for need, text in hooks if not need or facts.get(need)]
    hook = options[_variant(item, len(options), "hook")].format(**facts) if options else ""
    parts = [hook, "", item.name, f"💎 {money(price)} · {CITY}"]
    sentences = benefit_sentences(item)
    if sentences:
        parts += ["", " ".join(sentences)]
    bullets = feature_bullets(item)
    # Один пункт выглядит как случайный огрызок: показываем список от двух.
    if len(bullets) >= 2:
        parts += [""] + [f"✓ {line}" for line in bullets]
    ctas = CTAS.get(item.group, _ASK)
    parts += ["", ctas[_variant(item, len(ctas), "cta")], f"📞 {PHONE}"]
    return "\n".join(parts).strip()


def is_postable(item: CatalogItem) -> bool:
    """Пост без конкретики — пустая реклама: нужна цифра в описании или пара пунктов."""
    concrete = any(_NUM_UNIT.search(sentence) for sentence in benefit_sentences(item))
    return concrete or len(feature_bullets(item)) >= 2


# ── живая проверка карточки на сайте ────────────────────────────────────────

@dataclass(frozen=True)
class LiveInfo:
    price: int
    in_stock: bool


_LD = re.compile(r'<script type="application/ld\+json">(.*?)</script>', re.S)


def parse_live(html: str) -> LiveInfo | None:
    """Достать цену и наличие из разметки schema.org на странице товара."""
    for block in _LD.findall(html):
        try:
            data = json.loads(block)
        except ValueError:
            continue
        for node in (data if isinstance(data, list) else [data]):
            offers = node.get("offers") if isinstance(node, dict) else None
            if not offers:
                continue
            offers = offers[0] if isinstance(offers, list) else offers
            try:
                price = int(float(str(offers.get("price", "0"))))
            except ValueError:
                continue
            stock = "instock" in str(offers.get("availability", "")).casefold()
            return LiveInfo(price=price, in_stock=stock)
    # Запасной разбор для страниц, где разметка лежит не в offers.
    match = re.search(r'"price":\s*"([0-9]+)".{0,200}?"availability":\s*"([^"]+)"', html, re.S)
    if match:
        return LiveInfo(int(match.group(1)), "instock" in match.group(2).casefold())
    return None


def live_check(client: httpx.Client, item: CatalogItem) -> LiveInfo | None:
    try:
        response = client.get(item.url, headers={"User-Agent": "Mozilla/5.0"}, timeout=25)
    except httpx.HTTPError:
        return None
    if response.status_code != 200:
        return None
    return parse_live(response.text)


# ── выбор: какая группа и какой товар в какой слот ──────────────────────────

def group_for_slot(ordinal: int, day: date) -> str:
    """Группа товара для слота. Последовательность золотого сечения ровно
    попадает в сезонные веса без случайности, поэтому календарь считается наперёд."""
    weights = MONTH_WEIGHTS[day.month]
    total = sum(weights.values())
    point = ((ordinal * GOLDEN) % 1.0) * total
    run = 0.0
    for group in sorted(weights):
        run += weights[group]
        if point < run:
            return group
    return sorted(weights)[-1]


def pick_item(items: list[CatalogItem], group: str, taken_ids: set[str],
              recent_brands: list[str], salt: str = "") -> CatalogItem | None:
    pool = sorted((i for i in items if i.group == group and i.id not in taken_ids
                   and is_postable(i)),
                  key=lambda i: hashlib.sha1((i.id + salt).encode()).hexdigest())
    if not pool:
        return None
    avoid = set(recent_brands[-2:])
    for item in pool:
        if item.brand not in avoid:
            return item
    return pool[0]


# ── фото: светлый фон и поля, чтобы товар выглядел как в карточке магазина ──

PHOTO_SIZE = 1080
# Чисто белый: у студийных снимков производителей фон белый, и любой другой тон
# проступает вокруг товара видимой рамкой.
PHOTO_BACKGROUND = (255, 255, 255)
MIN_SOURCE_SIDE = 280


def prepare_photo(client: httpx.Client, url: str, destination: str | Path) -> Path | None:
    """Скачать фото производителя и положить на светлый квадрат; None — не годится."""
    from io import BytesIO

    from PIL import Image

    try:
        response = client.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=40,
                              follow_redirects=True)
        response.raise_for_status()
        source = Image.open(BytesIO(response.content))
        source.load()
    except (httpx.HTTPError, OSError, ValueError):
        return None
    if min(source.size) < MIN_SOURCE_SIDE:
        return None
    rgba = source.convert("RGBA")
    inner = int(PHOTO_SIZE * 0.84)
    rgba.thumbnail((inner, inner), Image.Resampling.LANCZOS)
    canvas = Image.new("RGB", (PHOTO_SIZE, PHOTO_SIZE), PHOTO_BACKGROUND)
    canvas.paste(rgba, ((PHOTO_SIZE - rgba.width) // 2, (PHOTO_SIZE - rgba.height) // 2), rgba)
    out = Path(destination)
    out.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out, "JPEG", quality=90)
    return out
