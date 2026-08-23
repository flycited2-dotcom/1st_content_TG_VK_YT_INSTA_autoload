from dataclasses import replace

from PIL import Image

from content_factory.orchestrator.vk_content_plan import VkPlanCandidate
from content_factory.publish.orders import OrderLinks
from content_factory.storefront.vk_catalog import (
    VkStorefrontStore,
    build_items,
    select_anchor_candidates,
    write_manifest,
)


def make_candidate(path, key="one", brand="DAICHI", category="air_conditioners", ts=1):
    return VkPlanCandidate(
        source_key=key, source_ts=ts,
        caption=f"{brand} Модель\n\n💎 25 790 ₽\n\n✅ Характеристика",
        card_path=str(path), category=category, brand=brand,
    )


def test_storefront_accepts_square_cover_and_rejects_cropped_shape(tmp_path):
    square = tmp_path / "square.png"
    vertical = tmp_path / "vertical.png"
    Image.new("RGB", (1254, 1254), "white").save(square)
    Image.new("RGB", (1080, 1350), "white").save(vertical)

    items, rejected = build_items(
        [make_candidate(square), make_candidate(vertical, key="two")],
        limit=20, public_image_base="https://example.test/cards",
        order_bot="OrderBot", order_links=OrderLinks(tmp_path / "orders.db"),
    )

    assert len(items) == 1
    assert items[0].image_width == items[0].image_height == 1254
    assert items[0].price == 25790
    assert items[0].order_url.startswith("https://t.me/OrderBot?start=ord_")
    assert "не квадратная 1080x1350" in rejected[0]


def test_anchor_selection_rotates_brands(tmp_path):
    candidates = [
        make_candidate(tmp_path / "unused", key="1", brand="A", ts=4),
        make_candidate(tmp_path / "unused", key="2", brand="A", ts=3),
        make_candidate(tmp_path / "unused", key="3", brand="B", ts=2),
    ]
    selected = select_anchor_candidates(candidates, 3)
    assert [item.brand for item in selected] == ["A", "B", "A"]


def test_storefront_store_tracks_price_change_and_missing_item(tmp_path):
    image = tmp_path / "square.png"
    Image.new("RGB", (100, 100), "white").save(image)
    items, _ = build_items(
        [make_candidate(image)], limit=20, public_image_base="https://example.test/cards",
        order_bot="OrderBot", order_links=OrderLinks(tmp_path / "orders.db"),
    )
    store = VkStorefrontStore(tmp_path / "state.db")

    assert store.sync(items) == {"added": 1, "changed": 0, "removed": 0}
    assert store.sync(items) == {"added": 0, "changed": 0, "removed": 0}
    changed = replace(items[0], price=26990)
    assert store.sync([changed]) == {"added": 0, "changed": 1, "removed": 0}
    assert store.sync([]) == {"added": 0, "changed": 0, "removed": 1}


def test_manifest_keeps_empty_collection_structure(tmp_path):
    output = tmp_path / "manifest.json"
    write_manifest(output, [], [])
    text = output.read_text(encoding="utf-8")
    assert "Стабилизаторы напряжения" in text
    assert '"Стабилизаторы напряжения": 0' in text
