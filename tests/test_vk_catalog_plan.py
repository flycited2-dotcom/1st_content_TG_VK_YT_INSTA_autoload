"""План товарных постов из каталога: чередование слотов, живая проверка, цена и наличие."""
from datetime import datetime, timedelta

from content_factory.orchestrator.vk_catalog_plan import (
    materialize_catalog_plan,
    refresh_catalog_items,
)
from content_factory.orchestrator.vk_content_plan import (
    VkContentPlanStore,
    plan_slots,
    slot_kind,
)
from content_factory.storefront.product_posts import CatalogItem, LiveInfo

PROSE = "Ночной режим работает при уровне шума от 24 дБ(А) и не мешает спать."


def _items(count=40):
    groups = ["ac", "heater", "radiator", "water", "vent", "air", "floor"]
    out = []
    for n in range(count):
        group = groups[n % len(groups)]
        out.append(CatalogItem(
            id=f"id{n}", url=f"https://splithome.ru/product/id{n}/", price=9000 + n,
            group=group, category="Бытовые сплит-системы", picture=f"https://img/{n}.png",
            name=f"Товар {n}", brand=f"brand{n % 5}", prose=PROSE, attrs={}))
    return out


def _fetch_ok(client, item):
    return LiveInfo(price=item.price, in_stock=True)


def _photo_ok(client, url, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(b"jpg")
    return destination


def test_slots_alternate_between_product_and_expert_posts():
    now = datetime(2026, 10, 1, 8, 0)
    kinds = [slot_kind(slot) for slot in plan_slots(now, horizon_days=14)]

    assert kinds[:6] in (["product", "editorial"] * 3, ["editorial", "product"] * 3)
    assert abs(kinds.count("product") - kinds.count("editorial")) <= 1


def test_catalog_posts_fill_product_slots_with_live_checked_items(tmp_path):
    store = VkContentPlanStore(tmp_path / "plan.db")
    now = datetime(2026, 10, 1, 8, 0)

    added = materialize_catalog_plan(
        store, _items(), now, client=None, photo_dir=tmp_path / "photos",
        fill_editorial_gaps=False, fetch=_fetch_ok, photo=_photo_ok)

    plan = store.list()
    assert added and len(plan) == len(added)
    assert all(slot_kind(item.due_at) == "product" for item in plan)
    assert all(item.source_key.startswith("catalog:") and item.content_type == "product"
               for item in plan)
    assert all("splithome.ru/product/" in item.caption and "utm_campaign=catalog_post" in item.caption
               for item in plan)
    assert len({item.source_key for item in plan}) == len(plan), "позиция не повторяется"


def test_out_of_stock_or_photoless_items_are_skipped_not_posted(tmp_path):
    store = VkContentPlanStore(tmp_path / "plan.db")
    now = datetime(2026, 10, 1, 8, 0)

    def fetch(client, item):
        return LiveInfo(price=item.price, in_stock=item.id != "id0")

    def photo(client, url, destination):
        return None if url.endswith("/1.png") else _photo_ok(client, url, destination)

    materialize_catalog_plan(store, _items(), now, client=None, photo_dir=tmp_path / "p",
                             fill_editorial_gaps=False, fetch=fetch, photo=photo)

    keys = {item.source_key for item in store.list()}
    assert "catalog:id0" not in keys and "catalog:id1" not in keys


def test_post_uses_the_live_price_not_the_catalog_snapshot(tmp_path):
    store = VkContentPlanStore(tmp_path / "plan.db")

    materialize_catalog_plan(
        store, _items(10), datetime(2026, 10, 1, 8, 0), client=None, photo_dir=tmp_path / "p",
        fill_editorial_gaps=False, photo=_photo_ok,
        fetch=lambda client, item: LiveInfo(price=12345, in_stock=True))

    assert all("12 345 ₽" in item.caption for item in store.list())


def test_refresh_blocks_vanished_items_and_reprices_changed_ones(tmp_path):
    store = VkContentPlanStore(tmp_path / "plan.db")
    now = datetime(2026, 10, 1, 8, 0)
    materialize_catalog_plan(store, _items(), now, client=None, photo_dir=tmp_path / "p",
                             fill_editorial_gaps=False, fetch=_fetch_ok, photo=_photo_ok)
    soon = [item for item in store.list()
            if now < datetime.fromtimestamp(item.due_at) <= now + timedelta(hours=72)]
    assert len(soon) >= 2
    gone, repriced = soon[0], soon[1]
    store.mark_review(repriced.id, 77)

    def fetch_url(url):
        if gone.caption.count(url.split("?")[0]):
            return LiveInfo(price=1, in_stock=False)
        return LiveInfo(price=50000, in_stock=True)

    result = refresh_catalog_items(store, client=None, now=now, fetch_url=fetch_url)

    assert gone.id in result["blocked"]
    assert store.get(gone.id).status == "blocked_unavailable"
    assert repriced.id in result["repriced"]
    after = store.get(repriced.id)
    assert "50 000 ₽" in after.caption
    assert after.status == "planned" and after.telegram_message_id is None, \
        "показанная карточка с новой ценой возвращается на ревью"


def test_realign_moves_expert_posts_out_of_product_slots(tmp_path):
    """До чередования эксперты занимали все слоты подряд и товарам места не оставалось."""
    from content_factory.orchestrator.vk_content_plan import VkPlanCandidate

    store = VkContentPlanStore(tmp_path / "plan.db")
    now = datetime(2026, 10, 1, 8, 0)
    slots = plan_slots(now, horizon_days=14)
    for index, slot in enumerate(slots[:10]):
        store.add(VkPlanCandidate(
            source_key=f"editorial:topic{index}:{index}", source_ts=1.0, caption=f"Текст {index}",
            card_path="/x.png", category="ups", brand="EDITORIAL", content_type="useful"), slot)

    result = store.realign_editorial_slots(slots, int(now.timestamp()))

    plan = store.list()
    active = [item for item in plan if item.status != "superseded"]
    assert all(slot_kind(item.due_at) == "editorial" for item in active)
    assert len(active) + len(result["dropped"]) == 10
    assert store.realign_editorial_slots(slots, int(now.timestamp())) == {"moved": [], "dropped": []}, \
        "повторный запуск ничего не меняет"


def test_realign_keeps_seasonal_posts_and_drops_old_checklists_first(tmp_path):
    from content_factory.orchestrator.vk_content_plan import VkPlanCandidate

    store = VkContentPlanStore(tmp_path / "plan.db")
    now = datetime(2026, 10, 1, 8, 0)
    slots = plan_slots(now, horizon_days=14)
    editorial_slots = [s for s in slots if slot_kind(s) == "editorial"]
    # Мест ровно на столько, сколько экспертных слотов; последним ставим сезонный пост.
    for index in range(len(editorial_slots)):
        store.add(VkPlanCandidate(
            source_key=f"editorial:old{index}:{index}", source_ts=1.0, caption=f"Старый {index}",
            card_path="/x.png", category="ups", brand="EDITORIAL", content_type="useful"),
            editorial_slots[index])
    store.add(VkPlanCandidate(
        source_key="editorial:season-x:99", source_ts=1.0, caption="Сезонный",
        card_path="/x.png", category="ups", brand="EDITORIAL", content_type="useful"), slots[-1])

    rank = lambda item: 0 if "season" in item.source_key else 2  # noqa: E731
    result = store.realign_editorial_slots(slots, int(now.timestamp()), rank=rank)

    season = [i for i in store.list() if "season" in i.source_key][0]
    assert season.status != "superseded" and slot_kind(season.due_at) == "editorial"
    assert len(result["dropped"]) == 1
    assert "season" not in store.get(result["dropped"][0]).source_key
