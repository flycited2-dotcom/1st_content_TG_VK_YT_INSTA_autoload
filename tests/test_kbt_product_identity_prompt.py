from pathlib import Path

from content_factory.config import load_config


def test_stabilizers_use_kbt_and_prompt_locks_product_geometry():
    root = Path(__file__).parent.parent
    cfg = load_config(root / "config" / "stabilizers-b2c.yaml")
    prompt = (root / "prompts" / "kbt-product-identity-addon.txt").read_text(
        encoding="utf-8")
    assert cfg.default_card_mode == "kbt"
    assert cfg.cards_modes_by_category[10598] == "kbt"
    assert "неизменяемым геометрическим паспортом" in prompt
    assert "водяные знаки поставщика" in prompt
    assert "логотип производителя" in prompt
