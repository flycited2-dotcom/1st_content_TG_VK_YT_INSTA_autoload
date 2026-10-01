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

PROSE = ("Ночной режим работает при уровне шума от 24 дБ(А) и не мешает спать. "
    "Расширенная гарантия на прибор составляет 5 лет.")


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


def _yml_dir(tmp_path):
    folder = tmp_path / "yml"
    folder.mkdir()
    (folder / "01.yml").write_text(
        '<?xml version="1.0" encoding="utf-8"?><yml_catalog><shop><categories>'
        '<category id="1">Бытовые сплит-системы</category></categories><offers>'
        '<offer id="yml:1"><url>https://splithome.ru/product/a/</url><price>25000</price>'
        '<categoryId>1</categoryId><picture>https://img/a.png</picture><name>Сплит-система A</name>'
        '<description>Описание.</description></offer></offers></shop></yml_catalog>',
        encoding="utf-8")
    return folder


def _snapshot_file(tmp_path):
    import json
    path = tmp_path / "snap.json"
    path.write_text(json.dumps([{
        "offer_id": "breeze:НС-1", "slug": "s-1", "title": "XIGMA Сплит-система S1", "price": 20000,
        "category": "Бытовые сплит-системы", "description": "Описание.", "picture": "https://img/s.png",
        "specs": {}, "is_heat_pump": False, "heating_min_temp": None}], ensure_ascii=False),
        encoding="utf-8")
    return path


def test_site_snapshot_is_preferred_over_the_old_yml_export(tmp_path):
    from content_factory.orchestrator.vk_catalog_plan import load_catalog_items

    items, report = load_catalog_items(_yml_dir(tmp_path), _snapshot_file(tmp_path),
                                       refresh=lambda path: {"status": "refreshed"})

    assert [item.id for item in items] == ["breeze:NS-1"]
    assert report == {"status": "refreshed", "source": "site"}


def test_a_crashing_refresh_falls_back_to_yml_and_never_raises(tmp_path):
    from content_factory.orchestrator.vk_catalog_plan import load_catalog_items

    def broken(path):
        raise RuntimeError("docker недоступен")

    items, report = load_catalog_items(_yml_dir(tmp_path), tmp_path / "нет.json", refresh=broken)

    assert [item.id for item in items] == ["yml:1"]
    assert report["source"] == "yml" and report["status"].startswith("failed")


class _FakePublisher:
    def __init__(self, ok=True, dry_run=False):
        self.calls, self.ok, self.dry_run = [], ok, dry_run

    def edit_text(self, post_id, caption, *, publish_at=None):
        from types import SimpleNamespace
        self.calls.append(dict(post_id=post_id, caption=caption, publish_at=publish_at))
        return SimpleNamespace(ok=self.ok, dry_run=self.dry_run, error="" if self.ok else "нет прав")


def _published_item(store, tmp_path, due, status="published_unverified"):
    from content_factory.orchestrator.vk_content_plan import VkPlanCandidate
    item_id = store.add(VkPlanCandidate(
        source_key="catalog:breeze:NS-9", source_ts=1.0,
        caption="Товар\n💎 9 990 ₽\n\n🛒 Смотреть и заказать: https://splithome.ru/product/x/?utm=1",
        card_path="/x.jpg", category="heaters", brand="ROYAL", content_type="product"), due)
    with store._connect() as connection:
        connection.execute("UPDATE vk_content_plan SET status=?,vk_post_id=77 WHERE id=?", (status, item_id))
    return item_id


def test_sold_out_product_is_marked_on_the_published_wall_post_and_restored_later(tmp_path):
    """Человек долистал до поста недельной давности, а товара уже нет."""
    from content_factory.orchestrator.vk_catalog_plan import reconcile_published

    store = VkContentPlanStore(tmp_path / "plan.db")
    now = datetime(2026, 10, 10, 12, 0)
    item_id = _published_item(store, tmp_path, int((now - timedelta(days=6)).timestamp()))
    compose = lambda item: item.caption  # noqa: E731
    publisher = _FakePublisher()

    gone = reconcile_published(store, publisher, now, compose,
                               fetch_url=lambda url: LiveInfo(price=0, in_stock=False))

    assert gone["marked"] == [item_id]
    assert publisher.calls[0]["post_id"] == 77
    assert publisher.calls[0]["caption"].startswith("⛔ Товар закончился")
    assert publisher.calls[0]["publish_at"] is None, "вышедший пост правим без отложенной даты"
    again = reconcile_published(store, publisher, now, compose,
                                fetch_url=lambda url: LiveInfo(price=0, in_stock=False))
    assert again["marked"] == [] and len(publisher.calls) == 1, "повторно не правим"

    back = reconcile_published(store, publisher, now, compose,
                               fetch_url=lambda url: LiveInfo(price=9990, in_stock=True))
    assert back["restored"] == [item_id]
    assert not publisher.calls[-1]["caption"].startswith("⛔")


def test_a_postponed_vk_post_keeps_its_publish_date_when_edited(tmp_path):
    """wall.edit без publish_date превращает отложенную запись в опубликованную немедленно."""
    from content_factory.orchestrator.vk_catalog_plan import reconcile_published

    store = VkContentPlanStore(tmp_path / "plan.db")
    now = datetime(2026, 10, 10, 12, 0)
    future = int((now + timedelta(hours=5)).timestamp())
    _published_item(store, tmp_path, future, status="photo_pending")
    publisher = _FakePublisher()

    reconcile_published(store, publisher, now, lambda item: item.caption,
                        fetch_url=lambda url: LiveInfo(price=0, in_stock=False))

    assert publisher.calls[0]["publish_at"] == future


def test_failed_vk_edit_is_reported_and_not_remembered(tmp_path):
    from content_factory.orchestrator.vk_catalog_plan import reconcile_published

    store = VkContentPlanStore(tmp_path / "plan.db")
    now = datetime(2026, 10, 10, 12, 0)
    item_id = _published_item(store, tmp_path, int((now - timedelta(days=1)).timestamp()))
    publisher = _FakePublisher(ok=False)

    result = reconcile_published(store, publisher, now, lambda item: item.caption,
                                 fetch_url=lambda url: LiveInfo(price=0, in_stock=False))

    assert result["failed"] == [item_id] and result["marked"] == []
    assert store.last_event(item_id, ("marked_sold",)) is None, "неудачу не запоминаем: попробуем снова"
    assert len(publisher.calls) == 1, "после первой ошибки VK не долбим остальные записи"
