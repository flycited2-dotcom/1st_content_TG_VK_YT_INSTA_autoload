import hashlib
import json

import pytest

from content_factory.ingest.avito_manifest import (
    ManifestError, clean_usp, load_manifest, product_key_for,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"card-bytes" * 4
JPEG = b"\xff\xd8\xff" + b"jpeg-bytes" * 4
USP = "Масляный радиатор на 9 секций.\n\nНапишите в Avito, чтобы уточнить наличие."


def _sha(blob: bytes) -> str:
    return hashlib.sha256(blob).hexdigest()


def _card(root, rel="cards/item.png", blob=PNG):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(blob)
    return rel, _sha(blob)


def _raw(root, *, rel=None, blob=PNG, usp_text=USP, source_id="AV-1", sku="NC-1", **over):
    rel, sha = _card(root, rel or f"cards/{source_id}.png", blob)
    item = {
        "source_id": source_id, "sku": sku, "series_key": "", "category": "oil_radiator",
        "availability": "in_stock", "title": "Радиатор X", "brand": "Ballu",
        "card": {"path": rel, "sha256": sha, "provenance": "generated",
                 "generator": "fotogen", "job_id": "job-1"},
        "usp": {"kind": "generator_override", "text": usp_text,
                "source_ref": "manifest.json#AV-1", "sha256": _sha(usp_text.encode())},
        "price": {"final": 9990, "currency": "RUB", "kind": "exact",
                  "already_marked_up": True},
    }
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(item.get(key), dict):
            item[key] = {**item[key], **value}
        else:
            item[key] = value
    return item


def _manifest(tmp_path, items, **over):
    root = tmp_path / "src-cards"
    root.mkdir(exist_ok=True)
    data = {"schema_version": 1, "batch_id": "b-2026-08-30", "cards_root": str(root),
            "feed": {"url": "https://splithome.ru/static/avito-feed.xml",
                     "sha256": _sha(b"feed"), "fetched_at": "2026-08-30T21:30:00+03:00"},
            "items": items(root) if callable(items) else items}
    data.update(over)
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path, root


def _load(tmp_path, build, **over):
    path, root = _manifest(tmp_path, build, **over)
    return load_manifest(path)


def _reasons(loaded):
    return [s.reason for s in loaded.skipped]


def test_valid_item_is_accepted_with_identity_and_revision_keys(tmp_path):
    loaded = _load(tmp_path, lambda root: [_raw(root)])
    assert not loaded.skipped
    item = loaded.items[0]
    assert item.product_key == product_key_for("AV-1") == "avito|AV-1"
    assert len(item.import_key) == 16
    assert item.display_name == "Ballu Радиатор X"
    assert item.card_kind == "png"


def test_denied_and_unknown_categories_are_rejected(tmp_path):
    loaded = _load(tmp_path, lambda root: [
        _raw(root, source_id="AV-1", category="generator"),
        _raw(root, source_id="AV-2", sku="NC-2", category="voltage_stabilizer"),
        _raw(root, source_id="AV-3", sku="NC-3", category="refrigerator"),
    ])
    assert loaded.items == []
    assert _reasons(loaded) == ["category_denied", "category_denied", "category_unknown"]


def test_convector_and_breezer_pass_but_are_flagged_for_separate_decision(tmp_path):
    loaded = _load(tmp_path, lambda root: [
        _raw(root, source_id="AV-1", category="convector_electric"),
        _raw(root, source_id="AV-2", sku="NC-2", category="breezer"),
        _raw(root, source_id="AV-3", sku="NC-3", category="thermostat"),
    ])
    flags = {i.product_key: i.review_required for i in loaded.items}
    assert flags == {"avito|AV-1": True, "avito|AV-2": True, "avito|AV-3": False}


def test_not_in_stock_is_rejected(tmp_path):
    loaded = _load(tmp_path, lambda root: [
        _raw(root, source_id="AV-1", availability="on_order"),
        _raw(root, source_id="AV-2", sku="NC-2", availability=""),
    ])
    assert _reasons(loaded) == ["not_in_stock", "not_in_stock"]


def test_price_must_be_final_rub_and_positive(tmp_path):
    loaded = _load(tmp_path, lambda root: [
        _raw(root, source_id="AV-1", price={"already_marked_up": False}),
        _raw(root, source_id="AV-2", sku="NC-2", price={"currency": "USD"}),
        _raw(root, source_id="AV-3", sku="NC-3", price={"final": 0}),
        _raw(root, source_id="AV-4", sku="NC-4", price={"final": "9990"}),
    ])
    assert _reasons(loaded) == ["price_not_final", "price_currency", "price_invalid",
                                "price_invalid"]


def test_series_from_requires_two_priced_models_and_matching_minimum(tmp_path):
    loaded = _load(tmp_path, lambda root: [
        _raw(root, source_id="AV-1", price={"kind": "series_from", "final": 9990},
             models=[{"model": "A", "price": 12000}]),
        _raw(root, source_id="AV-2", sku="NC-2",
             price={"kind": "series_from", "final": 9990},
             models=[{"model": "A", "price": 12000}, {"model": "B", "price": 11000}]),
        _raw(root, source_id="AV-3", sku="NC-3",
             price={"kind": "series_from", "final": 11000},
             models=[{"model": "A", "price": 12000}, {"model": "B", "price": 11000}]),
    ])
    assert _reasons(loaded) == ["series_needs_two_priced_models",
                                "series_from_price_mismatch"]
    ok = loaded.items[0]
    assert ok.price_from and [m.price for m in ok.models] == [11000, 12000]


def test_single_model_price_never_becomes_from(tmp_path):
    loaded = _load(tmp_path, lambda root: [_raw(root)])
    assert loaded.items[0].price_from is False


def test_generated_card_provenance_is_mandatory(tmp_path):
    loaded = _load(tmp_path, lambda root: [
        _raw(root, source_id="AV-1", card={"provenance": "feed_photo"}),
        _raw(root, source_id="AV-2", sku="NC-2", card={"generator": "unknown"}),
        _raw(root, source_id="AV-3", sku="NC-3",
             card={"path": "https://splithome.ru/static/x.jpg"}),
    ])
    assert loaded.items == []
    assert _reasons(loaded) == ["card_not_generated", "card_not_generated",
                                "card_path_is_url"]


def test_unsafe_card_paths_are_rejected(tmp_path):
    outside = tmp_path / "outside.png"
    outside.write_bytes(PNG)
    loaded = _load(tmp_path, lambda root: [
        _raw(root, source_id="AV-1", card={"path": "../outside.png"}),
        _raw(root, source_id="AV-2", sku="NC-2", card={"path": str(outside)}),
        _raw(root, source_id="AV-3", sku="NC-3", card={"path": "cards/missing.png"}),
    ])
    assert _reasons(loaded) == ["card_path_traversal", "card_path_not_relative",
                                "card_file_missing"]


def test_card_bytes_must_match_hash_and_be_a_real_image(tmp_path):
    loaded = _load(tmp_path, lambda root: [
        _raw(root, source_id="AV-1", card={"sha256": "0" * 64}),
        _raw(root, source_id="AV-2", sku="NC-2", blob=b"not-an-image-at-all"),
        _raw(root, source_id="AV-3", sku="NC-3", blob=b""),
    ])
    assert _reasons(loaded) == ["card_sha_mismatch", "card_not_an_image", "card_empty"]


def test_jpeg_card_is_accepted(tmp_path):
    loaded = _load(tmp_path, lambda root: [
        _raw(root, source_id="AV-1", rel="cards/a.jpg", blob=JPEG)])
    assert loaded.items[0].card_kind == "jpeg"


def test_usp_source_must_be_known_and_hash_must_match(tmp_path):
    loaded = _load(tmp_path, lambda root: [
        _raw(root, source_id="AV-1", usp={"kind": "invented"}),
        _raw(root, source_id="AV-2", sku="NC-2", usp={"sha256": "0" * 64}),
        _raw(root, source_id="AV-3", sku="NC-3", usp={"source_ref": ""}),
    ])
    assert _reasons(loaded) == ["usp_kind_unknown", "usp_sha_mismatch",
                                "usp_source_ref_missing"]


def test_avito_call_to_action_is_dropped_whole_block(tmp_path):
    loaded = _load(tmp_path, lambda root: [_raw(root)])
    assert loaded.items[0].usp_text == "Масляный радиатор на 9 секций."


def test_platform_mention_without_call_is_ambiguous_and_fails_closed(tmp_path):
    text = "Продаём на Avito с 2019 года, 9 секций."
    loaded = _load(tmp_path, lambda root: [_raw(root, usp_text=text)])
    assert _reasons(loaded) == ["usp_platform_mention_ambiguous"]


def test_usp_empty_after_cleanup_is_rejected(tmp_path):
    loaded = _load(tmp_path, lambda root: [
        _raw(root, usp_text="Напишите в Avito для заказа.")])
    assert _reasons(loaded) == ["usp_empty_after_cleanup"]


def test_usp_html_from_feed_is_not_passed_through(tmp_path):
    loaded = _load(tmp_path, lambda root: [
        _raw(root, usp_text="<p>9 секций</p><br>Мощность 2 кВт")])
    assert "<" not in loaded.items[0].usp_text


def test_clean_usp_keeps_facts_and_reports_reason():
    text, why = clean_usp("Мощность 2 кВт.\n\nПишите в авито!")
    assert (text, why) == ("Мощность 2 кВт.", None)
    text, why = clean_usp("Пишите в авито!")
    assert why == "usp_empty_after_cleanup"


def test_duplicate_identity_fails_the_whole_batch(tmp_path):
    with pytest.raises(ManifestError, match="дубль source_id"):
        _load(tmp_path, lambda root: [_raw(root, source_id="AV-1", sku="NC-1"),
                                      _raw(root, source_id="AV-1", sku="NC-2")])
    with pytest.raises(ManifestError, match="дубль sku"):
        _load(tmp_path, lambda root: [_raw(root, source_id="AV-1", sku="NC-1"),
                                      _raw(root, source_id="AV-2", sku="NC-1")])


def test_structural_problems_reject_the_manifest(tmp_path):
    with pytest.raises(ManifestError, match="schema_version"):
        _load(tmp_path, lambda root: [_raw(root)], schema_version=99)
    with pytest.raises(ManifestError, match="feed.sha256"):
        _load(tmp_path, lambda root: [_raw(root)], feed={"url": "x"})
    with pytest.raises(ManifestError, match="batch_id"):
        _load(tmp_path, lambda root: [_raw(root)], batch_id="")
    with pytest.raises(ManifestError, match="cards_root"):
        _load(tmp_path, lambda root: [_raw(root)], cards_root="relative/path")


def test_revision_key_tracks_price_while_product_key_stays_stable(tmp_path):
    first = _load(tmp_path, lambda root: [_raw(root)]).items[0]
    second = _load(tmp_path, lambda root: [
        _raw(root, price={"final": 10990})]).items[0]
    assert first.product_key == second.product_key
    assert first.import_key != second.import_key
