from pathlib import Path

import yaml


CONFIG = Path(__file__).parents[1] / "config" / "vk-community.yaml"


def test_public_vk_description_covers_business_scope():
    data = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    description = data["description"].casefold()

    assert "бытовой и климатической техники" in description
    assert "проектные решения" in description
    assert "индивидуальные задачи" in description
    assert "в наличии и под заказ" in description
    assert "проверяем актуальные наличие и цену" in description
    assert "подтверждаем заказ" in description


def test_public_vk_description_does_not_expose_suppliers():
    description = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))["description"].casefold()

    assert not any(name in description for name in ("breez", "bris", "daichi", "русклимат"))
