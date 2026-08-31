import json

import pytest

from content_factory.ingest.avito_manifest import load_manifest
from content_factory.ingest.avito_manifest_build import (
    build_manifest, clean_specs, display_title, normalized_category, product_kind,
)

CARDS_ROOT = "/opt/oasis/staticfiles/avito-cards"


def _inv_item(**over):
    base = {
        "source_id": "bridge:breeze|ecostar|smile",
        "series_key": "breeze|ecostar|smile",
        "supplier_sku": "breeze:НС-1412029",
        "category_id": 30,
        "brand": "ECOSTAR",
        "series": "SMILE",
        "title": "Оборудование Ecostar Smile",
        "description": "Ecostar Smile — оборудование в наличии.\n\nГарантия производителя.",
        "description_override": None,
        "price": 9119,
        "price_kind": "from",
        "currency": "RUB",
        "stock": 2,
        "members": [
            {"sku": "breeze:НС-1412029", "model": "Электрический водонагреватель серии "
                                                  "SMILE EWH-SM50-RE", "final_price": 9119},
            {"sku": "breeze:НС-1412036", "model": "Электрический водонагреватель серии "
                                                  "SMILE EWH-SM80-RE", "final_price": 11209},
        ],
        "generated_card_path": f"{CARDS_ROOT}/breeze__НС-1412029.png",
        "generated_card_sha256": "9" * 64,
        "generator_job": {"id": 2153, "status": "done"},
        "card_job": {"key": "breeze__НС-1412029", "status": "done"},
    }
    base.update(over)
    return base


def _inventory(items):
    return {"feed_sha256": "a" * 64, "checked_at": "2026-08-31T00:00:00+00:00",
            "items": items}


def _build(items, **over):
    kwargs = dict(cards_root=CARDS_ROOT, batch_id="avito-2026-08-31")
    kwargs.update(over)
    return build_manifest(_inventory(items), **kwargs)


# ── тип товара берём из модели, а не из безликого заголовка ─────────────────────
def test_product_kind_is_cut_before_the_brand():
    assert product_kind("Водонагреватель Ballu BWH/S 80 Artendo DH", "Ballu") == \
        "Водонагреватель"


def test_product_kind_is_cut_before_the_series_word():
    assert product_kind("Электрический водонагреватель серии SMILE EWH-SM50-RE",
                        "ECOSTAR") == "Электрический водонагреватель"


def test_product_kind_handles_latin_c_in_serii():
    """В данных встречается «cерии» с латинской c — резать всё равно надо."""
    assert product_kind("Водонагреватель cерии OMEGA RWH-OM50-RE", "Royal Clima") == \
        "Водонагреватель"


def test_faceless_equipment_title_gets_the_real_kind():
    assert display_title("Оборудование Ecostar Smile", "Ecostar",
                         "Электрический водонагреватель", "water_heater") == \
        "Электрический водонагреватель Ecostar Smile"


def test_correct_title_is_left_alone():
    assert display_title("Масляный обогреватель Ballu Classic", "Ballu",
                         "Масляный радиатор", "oil_radiator") == \
        "Масляный обогреватель Ballu Classic"


# ── терморегуляторы лежат внутри «тёплых полов» и должны называться собой ───────
def test_thermostat_inside_underfloor_category_is_recognised():
    assert normalized_category(119, "Терморегулятор") == "thermostat"
    assert normalized_category(119, "Мат") == "underfloor_heating"


def test_thermostat_title_is_corrected_from_underfloor_heating():
    assert display_title("Тёплый пол Electrolux Etl 16", "Electrolux",
                         "Терморегулятор", "thermostat") == \
        "Терморегулятор Electrolux Etl 16"


# ── сборка манифеста ───────────────────────────────────────────────────────────
def test_series_with_two_models_becomes_series_from_at_minimum_price():
    manifest, skipped = _build([_inv_item()])
    assert skipped == []
    price = manifest["items"][0]["price"]
    assert price["kind"] == "series_from" and price["final"] == 9119
    assert price["already_marked_up"] is True
    assert len(manifest["items"][0]["models"]) == 2


def test_single_model_stays_exact():
    item = _inv_item(price_kind="exact", price=11649,
                     members=[{"sku": "s", "model": "Водонагреватель Royal Clima OMEGA",
                               "final_price": 11649}])
    manifest, skipped = _build([item])
    assert skipped == [] and manifest["items"][0]["price"]["kind"] == "exact"


def test_card_path_is_relative_to_cards_root():
    manifest, _ = _build([_inv_item()])
    assert manifest["items"][0]["card"]["path"] == "breeze__НС-1412029.png"
    assert manifest["items"][0]["card"]["provenance"] == "generated"


def test_item_without_generated_card_is_skipped_with_reason():
    _, skipped = _build([_inv_item(generated_card_path=None)])
    assert skipped[0]["reason"] == "no_generated_card"


def test_item_out_of_stock_is_skipped():
    _, skipped = _build([_inv_item(stock=0)])
    assert skipped[0]["reason"] == "not_in_stock"


def test_item_without_price_is_skipped():
    _, skipped = _build([_inv_item(price=0)])
    assert skipped[0]["reason"] == "no_price"


def test_unmapped_category_is_skipped_and_named():
    _, skipped = _build([_inv_item(category_id=999)])
    assert skipped[0]["reason"] == "category_not_in_batch"
    assert "999" in skipped[0]["detail"]


def test_series_from_whose_minimum_disagrees_with_price_is_skipped(tmp_path):
    """Расхождение цены серии с минимальной моделью — молча не чиним."""
    _, skipped = _build([_inv_item(price=8000)])
    assert skipped[0]["reason"] == "series_price_mismatch"


def test_override_usp_is_marked_as_generator_override():
    manifest, _ = _build([_inv_item(description_override="Ручной текст УТП.")])
    usp = manifest["items"][0]["usp"]
    assert usp["kind"] == "generator_override" and usp["text"] == "Ручной текст УТП."


def test_series_key_and_sku_survive_for_legacy_dedupe():
    manifest, _ = _build([_inv_item()])
    item = manifest["items"][0]
    assert item["series_key"] == "breeze|ecostar|smile"
    assert item["sku"] == "breeze:НС-1412029"


def test_duplicate_source_ids_are_rejected():
    with pytest.raises(ValueError, match="дубл"):
        _build([_inv_item(), _inv_item()])


# ── результат должен проходить строгую валидацию самого манифеста ───────────────
def test_built_manifest_loads_through_the_strict_validator(tmp_path):
    root = tmp_path / "cards"
    root.mkdir()
    blob = b"\x89PNG\r\n\x1a\n" + b"card" * 8
    (root / "breeze__НС-1412029.png").write_bytes(blob)
    import hashlib
    item = _inv_item(generated_card_sha256=hashlib.sha256(blob).hexdigest(),
                     generated_card_path=f"{root}/breeze__НС-1412029.png")
    manifest, _ = _build([item], cards_root=str(root))
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")

    loaded = load_manifest(path)
    assert len(loaded.items) == 1 and loaded.skipped == []
    built = loaded.items[0]
    assert built.display_name == "Электрический водонагреватель Ecostar Smile"
    assert built.category == "water_heater" and built.price_from is True


# ── цены в посте должны иметь ровно один источник ──────────────────────────────
def test_price_block_is_removed_from_the_usp_text():
    """Описание из фида несёт свой перечень цен — застывший снимок. Подпись печатает
    цены из price/models, поэтому дубль убираем: иначе в посте две цены на один товар,
    а после обновления фида они ещё и разойдутся."""
    text = ("Ecostar Smile — оборудование в наличии.\n\n"
            "Модели и цены в наличии:\n"
            "• EWH-SM50-RE — 9 119 ₽\n"
            "• EWH-SM80-RE — 11 209 ₽\n\n"
            "Самовывоз в Симферополе.")
    manifest, _ = _build([_inv_item(description=text)])
    usp = manifest["items"][0]["usp"]["text"]
    assert "Модели и цены в наличии" not in usp
    assert "9 119" not in usp
    assert "Самовывоз в Симферополе." in usp
    assert usp.startswith("Ecostar Smile — оборудование в наличии.")


def test_single_price_line_block_is_removed_too():
    text = ("Товар в наличии.\n\n"
            "Цена в наличии:\n"
            "• Терморегулятор ETA-16 — 3 066 ₽")
    manifest, _ = _build([_inv_item(
        description=text, price_kind="exact", price=3066,
        members=[{"sku": "s", "model": "Терморегулятор ETA-16", "final_price": 3066}])])
    assert "Цена в наличии" not in manifest["items"][0]["usp"]["text"]


def test_characteristics_block_survives():
    text = "Товар в наличии.\n\nХарактеристики:\n• Гарантия: 3 года"
    manifest, _ = _build([_inv_item(description=text)])
    assert "Характеристики:" in manifest["items"][0]["usp"]["text"]


def test_usp_sha_matches_the_text_that_actually_ships():
    from content_factory.ingest.avito_manifest import sha256_text
    manifest, _ = _build([_inv_item()])
    usp = manifest["items"][0]["usp"]
    assert usp["sha256"] == sha256_text(usp["text"])


# ── блок характеристик приходит из фида сырым ──────────────────────────────────
def _specs(*rows):
    return "Товар в наличии.\n\nХарактеристики:\n" + "\n".join(f"• {r}" for r in rows)


def test_absent_features_are_dropped():
    """Пост не перечисляет, чего у товара НЕТ."""
    out = clean_specs(_specs("Защита от перегрева: Да", "Инверторная технология: Нет"))
    assert "Защита от перегрева: Да" in out
    assert "Инверторная технология" not in out


def test_certificate_uuid_is_dropped():
    out = clean_specs(_specs("Гарантийный срок: 2 года",
                             "Пожарный сертификат: 4bbab1fc-fdba-11f0-b8e1-00505601218a"))
    assert "Пожарный сертификат" not in out
    assert "Гарантийный срок: 2 года" in out


def test_trailing_underscore_in_a_key_is_cleaned():
    out = clean_specs(_specs("Таймер на включение_: Да"))
    assert "Таймер на включение: Да" in out
    assert "включение_" not in out


def test_usp_field_is_expanded_into_separate_bullets():
    """Поле буквально называется «УТП» и склеено точками с запятой."""
    out = clean_specs(_specs("УТП: Защита от перегрева;Индикация включения"))
    assert "• Защита от перегрева" in out
    assert "• Индикация включения" in out
    assert "УТП:" not in out


def test_specs_block_disappears_when_nothing_useful_is_left():
    out = clean_specs(_specs("Инверторная технология: Нет", "Термометр на корпусе: Нет"))
    assert "Характеристики" not in out
    assert out.strip() == "Товар в наличии."


def test_text_without_specs_block_is_untouched():
    text = "Товар в наличии.\n\nСамовывоз в Симферополе."
    assert clean_specs(text) == text


def test_values_are_never_rewritten():
    """Единицы измерения не додумываем: 75 остаётся 75."""
    out = clean_specs(_specs("Макс. температура воды: 75"))
    assert "Макс. температура воды: 75" in out


def test_manifest_records_why_items_were_excluded():
    """Отсев происходит здесь, а отчёт владельцу печатает импортёр — значит причины
    должны доехать до него внутри манифеста, иначе 160 пропавших позиций молча исчезнут."""
    manifest, _ = _build([_inv_item(), _inv_item(supplier_sku="x", source_id="y",
                                                 generated_card_path=None)])
    excluded = manifest["excluded"]
    assert excluded["count"] == 1
    assert excluded["reasons"]["no_generated_card"] == 1


def test_cleanup_is_applied_when_building_the_manifest():
    text = _specs("Защита от перегрева: Да", "Инверторная технология: Нет")
    manifest, _ = _build([_inv_item(description=text)])
    usp = manifest["items"][0]["usp"]["text"]
    assert "Инверторная технология" not in usp and "Защита от перегрева: Да" in usp
