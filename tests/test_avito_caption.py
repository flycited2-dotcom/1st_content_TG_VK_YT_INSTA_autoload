from pathlib import Path

from content_factory.avito_caption import (
    REVIEW_NOTE, TG_CAPTION_LIMIT, build_caption, money, tg_len,
)
from content_factory.ingest.avito_manifest import ManifestItem, ModelPrice


def _item(**over):
    base = dict(product_key="avito|AV-1", import_key="rev1", source_id="AV-1", sku="NC-1",
                series_key="", category="oil_radiator", category_label="Масляный радиатор",
                review_required=False, display_name="Ballu Vario BOH/CB-09",
                card_path=Path("card.png"), card_sha256="0" * 64, card_kind="png",
                card_generator="fotogen", card_job_id="job-1",
                usp_text="9 секций, мощность 2 кВт.", usp_kind="generator_override",
                usp_source_ref="manifest.json#AV-1", usp_sha256="0" * 64,
                price_final=9990, price_kind="exact", currency="RUB", models=())
    base.update(over)
    return ManifestItem(**base)


def _series(count=12, base_price=11000):
    models = tuple(ModelPrice(f"Модель {i:02d}", base_price + i * 500) for i in range(count))
    return _item(price_kind="series_from", price_final=min(m.price for m in models),
                 models=models)


def test_tg_length_counts_utf16_units_of_visible_text():
    assert tg_len("💎") == 2                      # эмодзи вне BMP = 2 единицы UTF-16
    assert tg_len("<b>💎</b>") == 2               # разметка не считается
    assert tg_len("&amp;") == 1                   # сущность = один видимый символ


def test_first_two_lines_are_name_and_price_for_item_summary():
    result = build_caption(_item())
    lines = result.caption.splitlines()
    assert lines[0] == "Ballu Vario BOH/CB-09"
    assert lines[1] == "<blockquote>💎 <b>9 990 ₽</b></blockquote>"


def test_exact_price_never_gets_the_from_prefix():
    assert "от" not in build_caption(_item()).caption.splitlines()[1]


def test_series_shows_from_minimum_and_model_table():
    result = build_caption(_series(count=3))
    assert f"от {money(11000)}" in result.caption
    assert "Модели и цены:" in result.caption
    assert result.models_shown == 3


def test_html_from_source_text_is_escaped_not_executed():
    result = build_caption(_item(display_name="Ballu <Vario> & Co",
                                 usp_text="Мощность 2 кВт & 9 секций"))
    assert "&lt;Vario&gt; &amp; Co" in result.caption
    assert "Мощность 2 кВт &amp; 9 секций" in result.caption


def test_long_description_is_shortened_by_whole_blocks_keeping_price():
    long_usp = "\n\n".join(f"Блок {i}: подробное описание характеристики товара." * 3
                           for i in range(20))
    result = build_caption(_item(usp_text=long_usp))
    assert result.ok
    assert result.dropped and set(result.dropped) == {"usp_block"}
    assert money(9990) in result.caption
    assert tg_len(result.review_caption) <= TG_CAPTION_LIMIT


def test_model_table_is_shrunk_visibly_and_from_price_stays_minimum():
    result = build_caption(_series(count=20), limit=420)
    assert result.ok
    assert "и ещё" in result.caption            # сокращение таблицы видно читателю
    assert 2 <= result.models_shown < 20
    assert f"от {money(11000)}" in result.caption   # «от» — минимум по ПОЛНОМУ списку


def test_stored_caption_excludes_review_note_and_both_fit_the_limit():
    result = build_caption(_item())
    assert REVIEW_NOTE not in result.caption
    assert result.review_caption == result.caption + f"\n\n{REVIEW_NOTE}"
    assert tg_len(result.caption) <= TG_CAPTION_LIMIT
    assert tg_len(result.review_caption) <= TG_CAPTION_LIMIT


def test_impossible_caption_is_rejected_instead_of_being_cut():
    result = build_caption(_item(), limit=10)
    assert result.ok is False and result.reason == "caption_too_long"
    assert result.caption == ""


def test_cta_is_dropped_before_the_price_or_the_name():
    result = build_caption(_series(count=2), limit=120)
    assert result.ok
    assert "cta" in result.dropped
    assert result.caption.splitlines()[0] == "Ballu Vario BOH/CB-09"
    assert money(11000) in result.caption
