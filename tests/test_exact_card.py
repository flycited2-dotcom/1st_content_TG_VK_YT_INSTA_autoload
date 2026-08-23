from decimal import Decimal

from PIL import Image

from content_factory.content.exact_card import (
    ExactCardSpec, card_spec_for_offer, compose_exact_product_card,
)
from content_factory.models import Offer


def test_exact_compositor_preserves_aspect_ratio_and_angle(tmp_path):
    source = tmp_path / "source.png"
    template = tmp_path / "template.png"
    output = tmp_path / "card.png"
    Image.new("RGB", (400, 200), "red").save(source)
    Image.new("RGB", (1024, 1024), "black").save(template)

    manifest = compose_exact_product_card(
        source, template, output,
        ExactCardSpec("RUCELF", "Стабилизатор напряжения", "SRW-12000-D"),
    )

    assert output.is_file()
    assert Image.open(output).size == (1024, 1024)
    assert manifest["geometry_preserved"] is True
    assert manifest["placed_size"][0] / manifest["placed_size"][1] == 2
    assert manifest["rotation_degrees"] == 0
    assert manifest["perspective_transform"] is False


def test_card_spec_uses_exact_article_and_safe_features():
    offer = Offer(
        supplier_sku="storefront:1", source="storefront", brand="RUCELF",
        model="Стабилизатор RUCELF SRW-12000-D, 12000 ВА, однофазный, настенный",
        category_id=10598, attrs={"Артикул": "SRW-12000-D", "Гарантия": "36"},
        cost=Decimal("1"), retail_ref=Decimal("2"), stock=1, photos=["https://x/img.jpg"],
    )
    spec = card_spec_for_offer(offer)
    assert spec.model == "SRW-12000-D"
    assert "12000 ВА" in spec.metrics
    assert "Однофазный" in spec.features
    assert "Настенное исполнение" in spec.features

