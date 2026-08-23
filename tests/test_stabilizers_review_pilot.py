import json
from decimal import Decimal

from content_factory.models import Offer
from content_factory.pilots.stabilizers_review import (
    absolute_media_path, apparent_power_va, retired_markup, review_markup,
    select_pilot_offers,
)


def _offer(sku, power, price=10000, photos=None):
    return Offer(supplier_sku=sku, source="storefront", brand="Brand",
                 model=f"Стабилизатор {power}ВА", category_id=10598,
                 cost=Decimal("5000"), retail_ref=Decimal(str(price)), stock=1,
                 photos=photos if photos is not None else ["https://x/image.jpg"], attrs={"x": "y"})


def test_apparent_power_va_supports_va_and_kva():
    assert apparent_power_va("Модель 8000ВА") == 8000
    assert apparent_power_va("Модель 12 кВА") == 12000
    assert apparent_power_va("Без мощности") is None


def test_selects_three_nearest_power_bands_and_requires_media_price():
    offers = [_offer("a", 1000), _offer("b", 1500), _offer("c", 8000),
              _offer("d", 12000), _offer("no-photo", 5000, photos=[]),
              _offer("no-price", 2000, price=0)]
    selected = select_pilot_offers(offers)
    assert [o.supplier_sku for o in selected] == ["b", "c", "d"]


def test_review_markup_contains_only_manual_publish_or_reject():
    markup = json.loads(review_markup("abc123"))
    callbacks = [button["callback_data"] for button in markup["inline_keyboard"][0]]
    assert callbacks == ["approve:abc123", "reject:abc123"]


def test_review_markup_can_label_existing_post_replacement():
    markup = json.loads(review_markup("abc123", "✅ Заменить карточку"))
    assert markup["inline_keyboard"][0][0]["text"] == "✅ Заменить карточку"


def test_retired_markup_has_no_actionable_old_callback():
    markup = json.loads(retired_markup())
    assert markup["inline_keyboard"][0][0]["callback_data"] == "noop"


def test_review_media_path_is_absolute_for_bot_running_in_another_directory(tmp_path):
    card = tmp_path / "cards" / "item.png"
    card.parent.mkdir()
    card.write_bytes(b"PNG")
    assert absolute_media_path(card).is_absolute()
    assert absolute_media_path(card) == card.resolve()
