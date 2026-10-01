"""Товарные посты из каталога: продающий текст без выдумок, отбор розницы, сезонный календарь."""
from collections import Counter
from datetime import date
from io import BytesIO

import httpx
from PIL import Image

from content_factory.storefront.product_posts import (
    CatalogItem,
    benefit_sentences,
    facts_for_hook,
    feature_bullets,
    group_for_slot,
    is_postable,
    live_check,
    load_catalog,
    parse_live,
    pick_item,
    prepare_photo,
    write_post,
)

OFFER = (
    '<offer id="{id}" available="true"><url>https://splithome.ru/product/{id}/</url>'
    "<price>{price}</price><currencyId>RUR</currencyId><categoryId>{cat}</categoryId>"
    "<picture>https://img.example/{id}.png</picture><name>{name}</name>"
    "<description>{desc}</description></offer>"
)


def _write_catalog(tmp_path, offers):
    body = "".join(OFFER.format(**offer) for offer in offers)
    (tmp_path / "01.yml").write_text(
        '<?xml version="1.0" encoding="utf-8"?><yml_catalog><shop><categories>'
        '<category id="1">Бытовые сплит-системы</category>'
        '<category id="2">Полупромышленные сплит-системы</category>'
        '<category id="3">Бытовые увлажнители воздуха</category>'
        f"</categories><offers>{body}</offers></shop></yml_catalog>", encoding="utf-8")
    return tmp_path


def _item(**over):
    base = dict(id="x1", url="https://splithome.ru/product/x1/", price=24990, group="ac",
                category="Бытовые сплит-системы", picture="https://img.example/x1.png",
                name="ROYAL CLIMA Сплит-система RC-1", brand="royal", prose="", attrs={})
    base.update(over)
    return CatalogItem(**base)


def test_catalog_keeps_retail_and_drops_industrial_and_overpriced(tmp_path):
    root = _write_catalog(tmp_path, [
        dict(id="ok", price=25000, cat=1, name="Сплит-система A", desc="Описание."),
        dict(id="duct", price=25000, cat=1, name="Канальный кондиционер B", desc="Описание."),
        dict(id="semi", price=25000, cat=2, name="Сплит-система C", desc="Описание."),
        dict(id="dear", price=900000, cat=1, name="Сплит-система D", desc="Описание."),
    ])

    assert [item.id for item in load_catalog(root)] == ["ok"]


def test_benefit_sentences_prefer_concrete_numbers_and_skip_series_talk():
    prose = (
        "Сплит-система RC-1\n"
        "Линейка RC включает 6 моделей от 20 до 70 м2. "
        "Сетевой кабель докупается отдельно. "
        "Продукты бренда проходят многоуровневый контроль качества на всех этапах. "
        "Ночной режим работает при уровне шума от 24 дБ(А) и не мешает спать. "
        "Передняя панель выполнена в современном дизайне."
    )

    sentences = benefit_sentences(_item(prose=prose), limit=2)

    assert sentences[0].startswith("Ночной режим")
    joined = " ".join(sentences)
    assert "Линейка" not in joined and "кабель" not in joined and "бренда" not in joined


def test_series_area_is_not_presented_as_the_models_area():
    """«от 20 до 70 м²» относится к линейке: комнаты для конкретной модели из него не вывести."""
    prose = "Подберите модель от 20 до 70 м2. Работает до 25 м2 в жару."
    assert facts_for_hook(_item(group="ac", prose=prose))["area"] == ""
    air = facts_for_hook(_item(group="vent", prose="Хватает для помещений площадью до 30 м2."))
    assert air["area"] == "30"


def test_post_has_price_name_and_no_link_and_no_installation_for_a_humidifier():
    item = _item(group="air", name="Увлажнитель Ballu UHB-1", price=7990,
                 prose="Прибор увлажняет воздух производительностью 350 мл/ч в течение 14 часов.")

    text = write_post(item)

    assert "7 990 ₽" in text and "Увлажнитель Ballu UHB-1" in text
    assert "http" not in text, "ссылка добавляется на этапе публикации"
    assert "установ" not in text.casefold() and "монтаж" not in text.casefold()


def test_inverter_attribute_drives_the_ac_hook():
    item = _item(attrs={"Инверторная технология": "Да"},
                 prose="Класс энергоэффективности соответствует A++ по классификации.")

    assert "инвертор" in write_post(item).casefold()
    assert "инвертор" not in write_post(_item(attrs={"Инверторная технология": "Нет"},
                                              prose=item.prose)).casefold()


def test_feature_bullets_skip_negatives_and_a_lone_bullet_is_not_shown():
    item = _item(attrs={"Таймер на отключение": "Нет", "Тепловой насос": "Да"},
                 prose="Класс энергоэффективности соответствует A++ по классификации.")

    assert feature_bullets(item) == ["Работает на обогрев"]
    assert "✓" not in write_post(item)


def test_item_without_substance_is_not_postable():
    assert not is_postable(_item(prose="Продукты бренда проходят контроль качества на всех этапах."))
    assert not is_postable(_item(prose="Ночной режим работает при уровне шума от 24 дБ(А)."))
    assert is_postable(_item(prose="Ночной режим работает при уровне шума от 24 дБ(А). "
                                  "Расширенная гарантия на прибор составляет 5 лет."))


def test_live_page_markup_gives_price_and_stock():
    html = ('<script type="application/ld+json">{"@type":"Product","offers":'
            '{"price":"26590","availability":"https://schema.org/InStock"}}</script>')
    gone = html.replace("InStock", "OutOfStock")

    assert parse_live(html).price == 26590 and parse_live(html).in_stock
    assert not parse_live(gone).in_stock
    assert parse_live("<html></html>") is None


def test_live_check_treats_a_missing_page_as_unavailable():
    transport = httpx.MockTransport(lambda request: httpx.Response(404))
    with httpx.Client(transport=transport) as client:
        assert live_check(client, _item()) is None


def test_autumn_calendar_leans_to_heating_and_summer_to_air_conditioners():
    october = Counter(group_for_slot(n, date(2026, 10, 15)) for n in range(1000))
    july = Counter(group_for_slot(n, date(2026, 7, 15)) for n in range(1000))

    assert october["heater"] + october["radiator"] > october["ac"]
    assert july["ac"] > july["heater"] + july["radiator"]
    # Золотое сечение даёт доли, близкие к весам, а не случайный разброс.
    assert 440 <= july["ac"] <= 480


def test_pick_avoids_recent_brands_and_never_repeats_an_item():
    prose = ("Ночной режим работает при уровне шума от 24 дБ(А) и не мешает спать. "
             "Расширенная гарантия на прибор составляет 5 лет.")
    items = [_item(id=f"a{n}", brand="royal" if n % 2 else "ballu", prose=prose)
             for n in range(8)]

    first = pick_item(items, "ac", set(), ["royal", "royal"])
    again = pick_item(items, "ac", {first.id}, ["royal", "royal"])

    assert first.brand != "royal" and again.id != first.id
    assert pick_item(items, "ac", {i.id for i in items}, []) is None


def _png(size):
    buffer = BytesIO()
    Image.new("RGBA", size, (20, 90, 160, 255)).save(buffer, "PNG")
    return buffer.getvalue()


def test_photo_is_centered_on_a_light_square_and_tiny_sources_are_rejected(tmp_path):
    big = httpx.MockTransport(lambda request: httpx.Response(200, content=_png((800, 500))))
    tiny = httpx.MockTransport(lambda request: httpx.Response(200, content=_png((120, 90))))

    with httpx.Client(transport=big) as client:
        path = prepare_photo(client, "https://img.example/a.png", tmp_path / "a.jpg")
    with httpx.Client(transport=tiny) as client:
        assert prepare_photo(client, "https://img.example/b.png", tmp_path / "b.jpg") is None

    with Image.open(path) as image:
        assert image.size == (1080, 1080)
        assert image.getpixel((5, 5)) == (246, 247, 249) or max(image.getpixel((5, 5))) > 235


def test_accessories_and_cheap_junk_stay_off_the_shop_window(tmp_path):
    root = _write_catalog(tmp_path, [
        dict(id="ok", price=25000, cat=1, name="Сплит-система A", desc="Описание."),
        dict(id="drain", price=593, cat=1, name="Нагреватель дренажа BALLU ND-500мм", desc="Описание."),
        dict(id="cheap", price=900, cat=1, name="Сплит-система дешёвая", desc="Описание."),
        dict(id="bracket", price=4000, cat=1, name="Кронштейн для наружного блока", desc="Описание."),
    ])

    assert [item.id for item in load_catalog(root)] == ["ok"]


def test_filler_without_numbers_does_not_make_a_post():
    fluff = "Модель отличается классическим элегантным дизайном и гармонично вписывается в интерьер."

    assert benefit_sentences(_item(prose=fluff)) == [fluff]
    assert not is_postable(_item(prose=fluff))
    concrete = f"{fluff} Бак объёмом 9,4 л позволяет заливать воду раз в сутки."
    assert benefit_sentences(_item(prose=concrete)) == [
        "Бак объёмом 9,4 л позволяет заливать воду раз в сутки."]
    assert not is_postable(_item(prose=concrete)), "один факт — это не пост"
    assert is_postable(_item(prose=concrete + " Работа устройства составляет до 12 часов."))


def test_section_count_is_declined_properly():
    radiator = _item(group="radiator", category="Радиаторы отопления",
                     name="Радиатор Royal Thermo - 4 секц.",
                     prose="Теплоотдача секции выше на 5%, нагрев идёт быстрее.")
    text = write_post(radiator)

    assert "4 секций" not in text
    twelve = write_post(_item(group="radiator", category="Радиаторы отопления",
                              name="Радиатор Royal Thermo - 12 секц.", prose=radiator.prose))
    for sample, word in ((text, "4 секции"), (twelve, "12 секций")):
        assert word in sample or "секц" not in sample.split("\n")[0]


def test_heater_subtypes_get_their_own_hooks():
    prose = "Снижает теплопотери на 80% при открытом проёме."
    curtain = write_post(_item(group="heater", category="Тепловые завесы", prose=prose))
    gun = write_post(_item(group="heater", category="Тепловые пушки", prose=prose))

    assert "завес" in curtain.split("\n")[0].casefold()
    assert "пушк" in gun.split("\n")[0].casefold()
    assert "условия эксплуатации" not in write_post(
        _item(prose="Условия эксплуатации: температура окружающего воздуха от -20 °С до +40 °С."))


def test_every_site_category_with_stock_lands_in_a_group():
    """Названия категорий на сайте и в старой выгрузке разные: «Тёплый пол» и «Тёплые полы»."""
    from content_factory.storefront.product_posts import group_of

    assert group_of("Тёплый пол") == "floor" and group_of("Тёплые полы") == "floor"
    assert group_of("Накопительные водонагреватели") == "water"
    assert group_of("Воздухоочистители") == "air"


def test_a_single_line_about_the_product_line_does_not_make_a_post():
    """Карточка «тепловая завеса и больше ничего» — владелец её забраковал: одно
    предложение про всю линейку (мощностью 3–6 кВт) и пустота вокруг."""
    curtain = _item(group="heater", category="Тепловые завесы",
                    name="ROYAL CLIMA Электрическая завеса HEATGUARD RAH-HG0.8E5M",
                    prose="Электрические завесы HEATGUARD - компактные тепловые завесы с электрическим "
                          "нагревом, мощностью 3-6 кВт. Снижает теплопотери на 80-90% при открытом проёме.")

    assert not is_postable(curtain)
    full = _item(group="heater", category="Тепловые завесы", attrs={
        "Защита от перегрева": "Да", "Таймер на отключение": "Да"}, prose=curtain.prose)
    assert is_postable(full), "с двумя пунктами характеристик тот же товар уже достоин поста"


def test_site_specs_become_facts_about_this_model_in_order_of_importance():
    """Из базы сайта приходят гарантия, срок службы, хладагент: факты именно об этой модели."""
    item = _item(attrs={
        "Гарантийный срок": "5 лет", "Хладагент": "R32", "Срок службы": "10 лет",
        "Инверторная технология": "Да", "Тепловой насос": "Да", "Страна производства": "КНР"})

    bullets = feature_bullets(item, limit=4)

    assert bullets[:2] == ["Работает на обогрев", "Инверторная технология"], \
        "то, ради чего покупают, идёт раньше гарантии"
    assert "Гарантия 5 лет" in bullets and "Хладагент R32" in bullets
    assert not any("КНР" in line for line in bullets), "страна производства — не довод к покупке"


def test_a_line_wide_power_range_is_not_presented_as_this_models_fact():
    prose = ("Тепловые завесы линейки мощностью 3-6 кВт подходят для разных проёмов. "
             "Ресурс нагревательного элемента составляет 25 лет.")

    assert benefit_sentences(_item(prose=prose)) == ["Ресурс нагревательного элемента составляет 25 лет."]


def test_group_follows_the_product_name_when_the_site_category_is_wrong():
    """В базе сайта 112 радиаторов числятся «Бытовыми сплит-системами»: пост про Zehnder
    открывался словами «Выбираете кондиционер?»."""
    from content_factory.storefront.product_posts import group_of_item

    wrong = "Бытовые сплит-системы"
    assert group_of_item(wrong, "Радиатор трубчатый Zehnder Charleston 2200") == "radiator"
    assert group_of_item(wrong, "Завеса воздушная Ballu BHC-U15A-PS2") == "heater"
    assert group_of_item("Масляные радиаторы", "Радиатор масляный Ballu Blaze BOH/BL-11B") == "heater"
    assert group_of_item("Компактные моноблочные вентиляционные установки",
                         "Очиститель воздуха приточный Ballu ONEAIR") == "air"
    assert group_of_item(wrong, "XIGMA Классическая сплит-система серии SKY") == "ac"
    assert group_of_item(wrong, "Нечто неопознанное") == "ac", "нет подсказки в названии — верим категории"


def test_a_misfiled_radiator_is_priced_and_worded_as_a_radiator(tmp_path):
    root = _write_catalog(tmp_path, [
        dict(id="rad", price=24000, cat=1, name="Радиатор трубчатый Zehnder Charleston 2200",
             desc="Описание.")])

    item = load_catalog(root)[0]

    assert item.group == "radiator"


def test_only_the_standard_heat_output_is_shown_for_a_radiator():
    """Три строки «Теплоотдача» с разным Δt и числом 694.416 Вт — не довод к покупке."""
    item = _item(group="radiator", attrs={
        "Теплоотдача при Δt 70": "552 Вт", "Теплоотдача при Δt 50": "694.416 Вт",
        "Теплоотдача при Δt 30": "849.6 Вт", "Гарантийный срок": "10 лет"})

    bullets = feature_bullets(item)

    assert bullets.count("Теплоотдача 552 Вт") == 1
    assert not any("694" in line or "849" in line for line in bullets)


def test_sentences_that_depend_on_a_missing_subject_are_not_used():
    """«Он хорошо подходит…» и «Практически нечувствительны…» ссылаются на предыдущую фразу,
    которой в посте нет: вырванные из контекста, они читаются как обрывок."""
    prose = ("Он хорошо подходит для локального обогрева комнат площадью до 20 м2. "
             "Практически нечувствительны к перепадам температур от -20 до +40 °С. "
             "Прибор прогревает помещение площадью до 20 м2 за 15 минут.")

    sentences = benefit_sentences(_item(prose=prose), limit=3)

    assert sentences == ["Прибор прогревает помещение площадью до 20 м2 за 15 минут."]


def test_a_water_convector_is_a_heating_system_part_not_a_room_heater(tmp_path):
    """«Конвектор напольный Royal Thermo STEP» с давлением 10 бар и теплоотдачей при Δt —
    водяной: он работает от системы отопления. Крючок про «согреть одну комнату» ему не подходит."""
    from content_factory.storefront.product_posts import refine_group

    water = {"Тип теплоносителя": "Вода", "Максимальное рабочее давление": "10 бар"}

    assert refine_group("heater", water) == "radiator"
    assert refine_group("heater", {"Защита от перегрева": "Да"}) == "heater"
    assert refine_group("ac", water) == "ac", "перекладываем только обогреватели"

    root = _write_catalog(tmp_path, [dict(
        id="conv", price=17000, cat=1, name="Конвектор напольный Royal Thermo STEP",
        desc="Описание.\n\nХарактеристики:\n• Тип теплоносителя: Вода\n• Максимальное рабочее давление: 10")])
    assert load_catalog(root)[0].group == "radiator"


def _png_of(size, ink_box=None, ink=(30, 30, 30)):
    """Белый фон; ink_box=(x0, y0, x1, y1) — тёмный прямоугольник-«товар»."""
    buffer = BytesIO()
    image = Image.new("RGB", size, (255, 255, 255))
    if ink_box:
        image.paste(Image.new("RGB", (ink_box[2] - ink_box[0], ink_box[3] - ink_box[1]), ink), ink_box[:2])
    image.save(buffer, "PNG")
    return buffer.getvalue()


def _client_with(images):
    return httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(
        200, content=images[str(request.url)])))


def test_best_of_several_photos_wins_over_white_on_white(tmp_path):
    """Первое фото приточки BREZZA — белый корпус на белом: товара почти не видно.
    Из четырёх снимков берётся тот, где товар различим."""
    images = {
        "https://img/front.png": _png_of((900, 900), (300, 300, 600, 600), ink=(250, 250, 250)),
        "https://img/angle.png": _png_of((900, 900), (200, 250, 700, 650), ink=(60, 60, 70)),
    }
    with _client_with(images) as client:
        path = prepare_photo(client, ["https://img/front.png", "https://img/angle.png"],
                             tmp_path / "p.jpg")

    with Image.open(path) as result:
        center = result.getpixel((540, 540))
    assert sum(center) < 400, "выбран снимок с различимым товаром, а не белое на белом"


def test_photo_is_cropped_to_the_product_so_it_fills_the_frame(tmp_path):
    """Конвектор-рендер занимал меньше половины кадра в пустом белом поле."""
    # Товар занимает лишь 30% ширины исходного снимка; без обрезки в кадре он остался бы
    # таким же мелким (около 270 пикселей из 1080), после обрезки растягивается на рамку.
    images = {"https://img/a.png": _png_of((1000, 1000), (350, 450, 650, 560))}
    with _client_with(images) as client:
        path = prepare_photo(client, "https://img/a.png", tmp_path / "p.jpg")

    with Image.open(path) as result:
        gray = result.convert("L")
        box = gray.point(lambda value: 255 if value < 200 else 0).getbbox()
    assert box[2] - box[0] >= 0.6 * 1080, "товар занимает основную часть ширины кадра"


def test_hydronic_convector_hook_and_cta_do_not_talk_about_radiator_sections():
    item = _item(group="radiator", category="Конвекторы", name="Конвектор напольный Royal Thermo STEP",
                 attrs={"Гарантийный срок": "10 лет", "Теплоотдача при Δt 70": "1140 Вт",
                        "Максимальное рабочее давление": "10 бар", "Срок службы": "25 лет"})

    text = write_post(item)

    assert "радиатор" not in text.casefold().split("конвектор напольный")[0], "крючок называет радиатором конвектор"
    assert "секци" not in text.casefold()
