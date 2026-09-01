from datetime import datetime
from pathlib import Path
import sqlite3

from content_factory.orchestrator.vk_content_plan import (
    VkContentPlanStore,
    VkPlanCandidate,
    callback_markup,
    choose_candidates,
    classify_category,
    format_vk_plan,
    handle_plan_callback,
    item_code,
    item_reference,
    materialize_plan,
    materialize_editorial_plan,
    photo_markup,
    photo_task_caption,
    plan_slots,
    review_caption,
    rotate_editorial_items,
)


def candidate(key: str, category: str, brand: str, ts: float = 1.0) -> VkPlanCandidate:
    return VkPlanCandidate(
        source_key=key,
        source_ts=ts,
        caption=f"{brand} {key}",
        card_path=f"/{key}.png",
        category=category,
        brand=brand,
    )


def test_plan_slots_make_two_slots_every_day_including_sunday():
    """Два выхода в день, семь дней в неделю — решение владельца 31.08.2026.

    Раньше был один пост в день пн–сб: лента выглядела пустой, а половина
    слотов уходила на товары, из-за чего полезные и сервисные материалы
    почти не появлялись.
    """
    slots = plan_slots(datetime(2026, 8, 24, 8, 0), horizon_days=14)
    dates = [datetime.fromtimestamp(value) for value in slots]

    assert len(dates) == 28
    assert {value.weekday() for value in dates} == set(range(7))
    assert [(value.hour, value.minute) for value in dates[:4]] == [
        (11, 30), (18, 30), (11, 30), (18, 30),
    ]
    # Оба времени приходятся на один и тот же день, а не чередуются через день.
    assert dates[0].date() == dates[1].date()


def test_classify_category_covers_current_store_assortment():
    assert classify_category("Стабилизатор напряжения Штиль") == "stabilizers"
    assert classify_category("Источник бесперебойного питания POWERMAN") == "ups"
    assert classify_category("Приточно-вытяжная вентиляция") == "ventilation"
    assert classify_category("Тепловой насос Daichi") == "heat_pumps"
    assert classify_category("Настенная сплит-система") == "air_conditioners"
    assert classify_category("Daichi Эйр 2 · 9000 BTU · до 25 м²") == "air_conditioners"


def test_choose_candidates_avoids_same_category_and_brand_when_possible():
    selected = choose_candidates([
        candidate("1", "stabilizers", "RUCELF", 4),
        candidate("2", "stabilizers", "RUCELF", 3),
        candidate("3", "ups", "POWERMAN", 2),
        candidate("4", "air_conditioners", "DAICHI", 1),
    ], 4)

    assert [item.source_key for item in selected] == ["1", "3", "2", "4"]
    assert all(
        current.category != previous.category and current.brand != previous.brand
        for previous, current in zip(selected, selected[1:])
    )


def test_materialize_plan_is_idempotent_and_callbacks_enforce_state(tmp_path):
    store = VkContentPlanStore(tmp_path / "plan.db")
    candidates = [candidate("one", "stabilizers", "RUCELF")]
    now = datetime(2026, 8, 24, 8, 0)

    added = materialize_plan(store, candidates, now)
    assert len(added) == 1
    assert materialize_plan(store, candidates, now) == []

    item_id = added[0]
    assert store.mark_review(item_id, 100)
    assert "одобрен" in handle_plan_callback(f"vkp:a:{item_id}", store)
    assert store.get(item_id).status == "approved"
    assert "уже обработан" in handle_plan_callback(f"vkp:a:{item_id}", store)
    assert store.mark_scheduled(item_id, 55)
    assert "прикреплённое" in handle_plan_callback(f"vkp:p:{item_id}", store)
    assert store.get(item_id).status == "photo_confirmed"


def test_review_and_photo_reminder_windows(tmp_path):
    store = VkContentPlanStore(tmp_path / "plan.db")
    now = datetime(2026, 8, 24, 8, 0)
    first, second = materialize_plan(store, [
        candidate("one", "stabilizers", "RUCELF", 2),
        candidate("two", "ups", "POWERMAN", 1),
    ], now)

    assert [item.id for item in store.for_review(int(now.timestamp()), limit=1)] == [first]
    assert store.mark_review(first, 100)
    assert store.approve(first)
    assert store.mark_scheduled(first, 77)
    due = store.get(first).due_at
    assert store.reminders(due - 3 * 3600 - 1) == []
    assert [item.id for item in store.reminders(due - 3 * 3600)] == [first]
    assert store.mark_reminded(first)
    assert store.reminders(due - 60) == []
    assert store.get(second).status == "planned"


def test_telegram_review_and_photo_task_have_same_human_identifier(tmp_path):
    store = VkContentPlanStore(tmp_path / "plan.db")
    now = datetime(2026, 8, 24, 8, 0)
    item_id = materialize_plan(store, [
        VkPlanCandidate(
            "breeze|xigma|sky", 1,
            "XIGMA Классическая сплит-система серии SKY\nЦена от 15 590 ₽",
            "/sky.jpg", "air_conditioners", "XIGMA",
        ),
    ], now)[0]
    assert store.mark_review(item_id, 100)
    assert store.approve(item_id)
    assert store.mark_scheduled(item_id, 9)
    item = store.get(item_id)

    assert item_code(item) == f"CF-VK-{item_id:03d}"
    assert item_reference(item) == (
        f"CF-VK-{item_id:03d} · XIGMA Классическая сплит-система серии SKY"
    )
    assert item_code(item) in review_caption(item)
    assert "XIGMA" in review_caption(item)
    assert "кондиционеры" in review_caption(item)
    assert item_code(item) in callback_markup(item)
    assert item_code(item) in photo_markup(item)
    task = photo_task_caption(item)
    assert item_code(item) in task
    assert "XIGMA" in task
    assert "VK №9" in task


def test_xigma_sky_duplicate_is_blocked_but_inverter_is_distinct(tmp_path):
    store = VkContentPlanStore(tmp_path / "plan.db")
    classic = VkPlanCandidate(
        "breeze|xigma|sky", 3,
        "XIGMA Классическая сплит-система серии SKY\n15 590 ₽",
        "/classic.jpg", "air_conditioners", "XIGMA",
    )
    same_classic = VkPlanCandidate(
        "other|xigma|sky", 2,
        "Классический кондиционер XIGMA SKY\n16 000 ₽",
        "/duplicate.jpg", "air_conditioners", "XIGMA",
    )
    inverter = VkPlanCandidate(
        "breeze|xigma|sky inverter", 1,
        "XIGMA Инверторная сплит-система серии SKY Inverter\n21 590 ₽",
        "/inverter.jpg", "air_conditioners", "XIGMA",
    )

    assert store.add(classic, 100) is not None
    assert store.add(same_classic, 200) is None
    assert store.add(inverter, 300) is not None
    assert [item.source_key for item in store.list()] == [
        "breeze|xigma|sky", "breeze|xigma|sky inverter",
    ]


def test_overdue_repair_moves_unsent_and_closes_photo_states(tmp_path):
    store = VkContentPlanStore(tmp_path / "plan.db")
    planned = store.add(candidate("planned", "stabilizers", "RUCELF"), 100)
    review = store.add(candidate("review", "ups", "POWERMAN"), 200)
    approved = store.add(candidate("approved", "air_conditioners", "XIGMA"), 300)
    confirmed = store.add(candidate("confirmed", "ventilation", "FUNAI"), 400)
    pending = store.add(candidate("pending", "heat_pumps", "DAICHI"), 500)
    future = store.add(candidate("future", "recuperators", "ROYAL"), 2000)
    assert store.mark_review(review, 10)
    assert store.mark_review(approved, 11) and store.approve(approved)
    assert store.mark_review(confirmed, 12) and store.approve(confirmed)
    assert store.mark_scheduled(confirmed, 77) and store.confirm_photo(confirmed)
    assert store.mark_review(pending, 13) and store.approve(pending)
    assert store.mark_scheduled(pending, 78)

    result = store.repair_overdue(600, [1000, 2000, 3000, 4000])

    assert result["moved"] == [
        {"id": planned, "from": 100, "to": 1000},
        {"id": review, "from": 200, "to": 3000},
        {"id": approved, "from": 300, "to": 4000},
    ]
    assert result["published_unverified"] == [confirmed]
    assert result["photo_overdue"] == [pending]
    assert store.get(review).status == "planned"
    assert store.get(review).telegram_message_id is None
    assert store.get(approved).status == "planned"
    assert store.get(confirmed).status == "published_unverified"
    assert store.get(pending).status == "photo_overdue"
    assert store.get(future).due_at == 2000
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM vk_plan_events").fetchone()[0] == 5


def test_overdue_without_free_slot_is_blocked_and_vkplan_explains_actions(tmp_path):
    store = VkContentPlanStore(tmp_path / "plan.db")
    now = datetime(2026, 8, 25, 12, 0)
    now_ts = int(now.timestamp())
    overdue = store.add(candidate("late", "stabilizers", "RUCELF"), now_ts - 200)
    pending = store.add(candidate("photo", "ups", "POWERMAN"), now_ts - 100)
    assert store.mark_review(pending, 20) and store.approve(pending)
    assert store.mark_scheduled(pending, 91)
    result = store.repair_overdue(now_ts, [])

    assert result["blocked"] == [overdue]
    text = format_vk_plan(store, now=now, owner_id=-241020718)
    assert "CF-VK" in text
    assert "свободного слота нет" in text
    assert "срок фото пропущен" in text
    assert "wall-241020718_91" in text
    assert "Действие:" in text


def test_catalog_change_forces_fresh_review_and_missing_item_is_blocked(tmp_path):
    store = VkContentPlanStore(tmp_path / "plan.db")
    now = datetime(2026, 8, 24, 8, 0)
    first, second = materialize_plan(store, [
        candidate("one", "stabilizers", "RUCELF", 2),
        candidate("two", "ups", "POWERMAN", 1),
    ], now)
    assert store.mark_review(first, 100)
    assert store.approve(first)

    fresh = candidate("one", "stabilizers", "RUCELF", 2)
    fresh = VkPlanCandidate(**{**fresh.__dict__, "caption": "RUCELF one\nНовая цена"})
    result = store.synchronize_candidates([fresh])

    assert result == {"changed": 1, "blocked": 1}
    assert store.get(first).status == "planned"
    assert store.get(first).telegram_message_id is None
    assert "Новая цена" in store.get(first).caption
    assert store.get(second).status == "blocked_unavailable"


def test_approved_item_is_not_scheduled_more_than_day_early(tmp_path):
    store = VkContentPlanStore(tmp_path / "plan.db")
    now = datetime(2026, 8, 24, 8, 0)
    # Два слота в день, поэтому первые два материала выходят сегодня и оба
    # попадают в сутки ожидания; отсечь должно третий — он уже завтра.
    first, second, third = materialize_plan(store, [
        candidate("one", "stabilizers", "RUCELF", 3),
        candidate("two", "ups", "POWERMAN", 2),
        candidate("three", "recuperators", "BREEZ", 1),
    ], now)
    for plan_id, message_id in ((first, 100), (second, 101), (third, 102)):
        assert store.mark_review(plan_id, message_id) and store.approve(plan_id)

    approved = store.approved(int(now.timestamp()), lead_hours=24)
    assert [item.id for item in approved] == [first, second]


def test_l2_auto_approves_low_risk_editorial_but_not_products_or_comparison(tmp_path):
    store = VkContentPlanStore(tmp_path / "plan.db")
    now = datetime(2026, 8, 24, 8, 0)
    useful_image = tmp_path / "useful.png"
    comparison_image = tmp_path / "comparison.png"
    useful_image.touch()
    comparison_image.touch()
    rows = [
        candidate("product", "stabilizers", "RUCELF"),
        VkPlanCandidate("editorial:useful", 2, "Совет", str(useful_image),
                        "climate", "EDITORIAL", "useful"),
        VkPlanCandidate("editorial:comparison", 1, "Сравнение", str(comparison_image),
                        "climate", "EDITORIAL", "comparison"),
    ]
    materialize_plan(store, rows, now, max_products=3)
    approved = store.auto_approve(("useful", "service", "trust"))
    assert len(approved) == 1
    statuses = {item.source_key: item.status for item in store.list()}
    assert statuses["editorial:useful"] == "approved"
    assert statuses["product"] == "planned"
    assert statuses["editorial:comparison"] == "planned"


def test_product_dedupe_blocks_same_model_from_another_source(tmp_path):
    store = VkContentPlanStore(tmp_path / "plan.db")
    first = VkPlanCandidate(
        "breeze:rucelf:srw12000", 2,
        "RUCELF SRW-12000-D — стабилизатор напряжения\n22 900 ₽",
        "/one.png", "stabilizers", "RUCELF", "product",
    )
    duplicate = VkPlanCandidate(
        "supplier2:rucelf:srw-12000-d", 1,
        "Стабилизатор RUCELF SRW 12000 D\nЦена 23 100 ₽",
        "/two.png", "stabilizers", "RUCELF", "product",
    )
    assert store.add(first, 100) is not None
    assert store.add(duplicate, 200) is None
    assert len(store.list()) == 1
    assert store.dedupe_counts()["registry"] == 1


def test_editorial_dedupe_ignores_punctuation_urls_and_cta(tmp_path):
    store = VkContentPlanStore(tmp_path / "plan.db")
    first = VkPlanCandidate(
        "editorial:first", 2,
        "Почему важно очищать фильтры кондиционера? Делайте это регулярно.\n"
        "🌐 https://splithome.ru/?utm_source=vk",
        "", "air_conditioners", "EDITORIAL", "useful",
    )
    duplicate = VkPlanCandidate(
        "editorial:second", 1,
        "Почему важно очищать фильтры кондиционера — делайте это регулярно!\n"
        "Напишите или позвоните нам.",
        "", "air_conditioners", "EDITORIAL", "useful",
    )
    assert store.add(first, 100) is not None
    assert store.add(duplicate, 200) is None


def test_publish_claim_is_single_use_but_releasable_after_api_error(tmp_path):
    store = VkContentPlanStore(tmp_path / "plan.db")
    item_id = store.add(candidate("one", "stabilizers", "RUCELF"), 100)
    assert store.claim_publication(item_id)
    assert not store.claim_publication(item_id)
    store.release_publication_claim(item_id)
    assert store.claim_publication(item_id)


def test_existing_active_duplicates_are_superseded_once_on_migration(tmp_path):
    path = tmp_path / "plan.db"
    store = VkContentPlanStore(path)
    first = VkPlanCandidate(
        "supplier:a", 2, "RUCELF SRW-12000-D стабилизатор", "/one.png",
        "stabilizers", "RUCELF", "product",
    )
    first_id = store.add(first, 100)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO vk_content_plan(source_key,due_at,category,brand,content_type,"
            "caption,card_path,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            ("supplier:b", 200, "stabilizers", "RUCELF", "product",
             "Стабилизатор RUCELF SRW 12000 D", "/two.png", "planned", 2, 2),
        )
        connection.execute("DELETE FROM vk_dedupe_registry")
    migrated = VkContentPlanStore(path)
    statuses = {item.source_key: item.status for item in migrated.list()}
    assert statuses["supplier:a"] == "planned"
    assert statuses["supplier:b"] == "superseded_duplicate"
    assert migrated.dedupe_counts()["blocked"] == 1
    # Повторная инициализация не должна заблокировать сохранённый оригинал.
    assert VkContentPlanStore(path).get(first_id).status == "planned"


def test_editorial_plan_rebalances_product_only_schedule(tmp_path):
    store = VkContentPlanStore(tmp_path / "plan.db")
    now = datetime(2026, 8, 24, 8, 0)
    products = [candidate(str(index), "air_conditioners", f"BRAND{index}", index)
                for index in range(20)]
    materialize_plan(store, products, now, max_products=20)

    store.rebalance_products(3, int(now.timestamp()))
    knowledge = Path(__file__).parents[1] / "config" / "vk-editorial-sources.yaml"
    added = materialize_editorial_plan(store, knowledge, now)
    active = [item for item in store.list() if item.status in {
        "visual_pending", "planned", "review", "approved", "photo_pending", "photo_confirmed",
    }]

    # Слотов стало вдвое больше, поэтому в план помещаются все редакционные темы,
    # а не девять: именно ради этого расписание и уплотняли. Счёт не зашит —
    # добавление темы в справочник не должно ронять тест.
    from content_factory.agents.editorial import load_ideas
    ideas, _ = load_ideas(knowledge)
    assert len(added) == len(ideas)
    assert len(active) == 3 + len(ideas)
    assert sum(item.content_type == "product" for item in active) == 3
    assert {item.content_type for item in active if item.content_type != "product"} == {
        "useful", "service", "comparison", "trust",
    }


def test_editorial_without_visual_cannot_reach_review_or_approval(tmp_path):
    store = VkContentPlanStore(tmp_path / "plan.db")
    now = datetime(2026, 8, 24, 8, 0)
    item_id = store.add(VkPlanCandidate(
        "editorial:visual", 1, "Полезный материал", "",
        "climate", "EDITORIAL", "useful",
    ), int(datetime(2026, 8, 24, 11, 30).timestamp()))

    assert store.get(item_id).status == "visual_pending"
    assert store.for_review(int(now.timestamp())) == []
    assert not store.approve(item_id)

    image = tmp_path / "visual.png"
    image.touch()
    assert store.attach_visual(item_id, image)
    assert store.get(item_id).status == "planned"
    assert [item.id for item in store.for_review(int(now.timestamp()))] == [item_id]


def test_editorial_visual_refresh_preserves_review_and_updates_dedupe(tmp_path):
    store = VkContentPlanStore(tmp_path / "plan.db")
    image = tmp_path / "visual.png"
    image.touch()
    item_id = store.add(VkPlanCandidate(
        "editorial:refresh", 1, "Старый текст", "",
        "climate", "EDITORIAL", "useful",
    ), 100)
    assert store.attach_visual(item_id, image)
    assert store.mark_review(item_id, 77)

    assert store.update_editorial_content(item_id, "Новый проверенный текст", image)
    item = store.get(item_id)
    assert item.status == "review"
    assert item.caption == "Новый проверенный текст"
    assert store.replace_review_message(item_id, 88)
    assert store.get(item_id).telegram_message_id == 88


def test_existing_editorial_post_can_be_refreshed_without_new_plan_item(tmp_path):
    store = VkContentPlanStore(tmp_path / "plan.db")
    image = tmp_path / "visual.png"
    image.touch()
    item_id = store.add(VkPlanCandidate(
        "editorial:clean-ac-filters:20260827", 1, "Старый короткий текст", str(image),
        "air_conditioners", "EDITORIAL", "useful",
    ), 100)
    assert store.mark_review(item_id, 77)
    assert store.approve(item_id)
    assert store.mark_scheduled(item_id, 16)
    with store._connect() as connection:
        connection.execute(
            "UPDATE vk_content_plan SET status='published_unverified' WHERE id=?", (item_id,),
        )

    assert store.update_editorial_content(
        item_id, "Полный проверенный чек-лист", image,
        content_type="service", category="air_conditioners",
    )
    item = store.get(item_id)
    assert item.status == "published_unverified"
    assert item.content_type == "service"
    assert item.caption == "Полный проверенный чек-лист"


def test_editorial_revision_stops_publication_and_keeps_comment(tmp_path):
    store = VkContentPlanStore(tmp_path / "plan.db")
    image = tmp_path / "visual.png"
    image.touch()
    item_id = store.add(VkPlanCandidate(
        "editorial:revision", 1, "Сервис кондиционера", str(image),
        "air_conditioners", "EDITORIAL", "service",
    ), 100)
    assert store.mark_review(item_id, 77)

    assert store.request_revision(
        item_id, "Мастер должен стоять на устойчивой стремянке", kind="image",
    )
    assert store.get(item_id).status == "revision_requested"
    assert store.revision_note(item_id) == "Мастер должен стоять на устойчивой стремянке"
    assert store.approved() == []
    assert "возвращён на доработку" in format_vk_plan(
        store, now=datetime(2026, 8, 24, 8, 0),
    )
    assert "устойчивой стремянке" in format_vk_plan(
        store, now=datetime(2026, 8, 24, 8, 0),
    )

    replacement = tmp_path / "visual-v2.png"
    replacement.touch()
    assert store.attach_visual(item_id, replacement)
    assert store.get(item_id).status == "planned"
    assert store.revision_note(item_id) == ""


def test_regenerate_photo_callback_creates_revision_request(tmp_path):
    store = VkContentPlanStore(tmp_path / "plan.db")
    image = tmp_path / "visual.png"
    image.touch()
    item_id = store.add(VkPlanCandidate(
        "editorial:regen", 1, "Полезный пост", str(image),
        "climate", "EDITORIAL", "useful",
    ), 100)
    assert store.mark_review(item_id, 70)

    answer = handle_plan_callback(f"vkp:g:{item_id}", store)
    assert "перегенерацию" in answer
    assert store.get(item_id).status == "revision_requested"
    assert "Перегенерировать" in store.revision_note(item_id)
    assert "vkp:g:" in callback_markup(item_id)
    assert "vkp:e:" in callback_markup(item_id)


def test_editorial_rotation_avoids_same_category_when_alternatives_exist():
    items = [
        VkPlanCandidate("ac1", 1, "", "x", "air_conditioners", "E", "useful"),
        VkPlanCandidate("ac2", 1, "", "x", "air_conditioners", "E", "useful"),
        VkPlanCandidate("ups", 1, "", "x", "ups", "E", "useful"),
        VkPlanCandidate("vent", 1, "", "x", "ventilation", "E", "useful"),
    ]
    rotated = rotate_editorial_items(items, "air_conditioners")

    assert [item.category for item in rotated] == [
        "ups", "air_conditioners", "ventilation", "air_conditioners",
    ]


def test_blocked_overdue_returns_to_the_plan_once_a_slot_frees(tmp_path):
    """«Свободного слота нет» — временная нехватка, а не выбывание из плана.

    В бою материал id 1 завис в blocked_overdue с 24.08 и остался там, хотя
    следом вышло несколько постов и слоты освободились: и выборка кандидатов,
    и UPDATE переноса перечисляли статусы явно, а blocked_overdue в списки не
    входил. Материал молча выпадал из контент-плана навсегда.
    """
    store = VkContentPlanStore(tmp_path / "plan.db")
    stuck = store.add(candidate("late", "stabilizers", "RUCELF"), 100)

    # Свободных слотов нет — материал блокируется.
    assert store.repair_overdue(600, [])["blocked"] == [stuck]
    assert store.get(stuck).status == "blocked_overdue"

    # Слот освободился — материал обязан вернуться в план.
    result = store.repair_overdue(700, [1000])

    assert result["moved"] == [{"id": stuck, "from": 100, "to": 1000}]
    assert store.get(stuck).status == "planned"
    assert store.get(stuck).due_at == 1000


def test_visual_gate_releases_material_once_the_frame_appears(tmp_path):
    """Шлюз обязан работать в обе стороны.

    Раньше материал без кадра уходил в visual_pending и оставался там даже
    после того, как изображение появлялось на диске: обратный переход делал
    только ручной attach_visual. В бою так зависли пять редакционных постов.
    """
    assets = tmp_path / "editorial"
    assets.mkdir()
    store = VkContentPlanStore(tmp_path / "plan.db")
    now_ts = int(datetime(2026, 9, 1, 12, 0).timestamp())
    item_id = store.add(
        VkPlanCandidate(source_key="editorial:ups-sine-wave:20260911", source_ts=1.0,
                        caption="Текст поста", card_path="", category="ups",
                        brand="EDITORIAL", content_type="useful"),
        now_ts + 3600,
    )

    # Материал без кадра попадает в шлюз уже при постановке в план.
    assert store.get(item_id).status == "visual_pending"
    assert store.require_editorial_visuals(assets) == []

    (assets / "ups-sine-wave.png").write_bytes(b"png")

    assert store.require_editorial_visuals(assets) == [item_id]
    restored = store.get(item_id)
    assert restored.status == "planned"
    assert restored.card_path.endswith("ups-sine-wave.png")
