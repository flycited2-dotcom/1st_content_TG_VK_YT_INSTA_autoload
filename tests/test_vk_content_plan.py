from datetime import datetime

from content_factory.orchestrator.vk_content_plan import (
    VkContentPlanStore,
    VkPlanCandidate,
    choose_candidates,
    classify_category,
    handle_plan_callback,
    materialize_plan,
    plan_slots,
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


def test_plan_slots_make_one_slot_monday_to_saturday_and_skip_sunday():
    slots = plan_slots(datetime(2026, 8, 24, 8, 0), horizon_days=14)
    dates = [datetime.fromtimestamp(value) for value in slots]

    assert len(dates) == 12
    assert all(value.weekday() != 6 for value in dates)
    assert [(value.hour, value.minute) for value in dates[:4]] == [
        (11, 30), (18, 30), (11, 30), (18, 30),
    ]


def test_classify_category_covers_current_store_assortment():
    assert classify_category("Стабилизатор напряжения Штиль") == "stabilizers"
    assert classify_category("Источник бесперебойного питания POWERMAN") == "ups"
    assert classify_category("Приточно-вытяжная вентиляция") == "ventilation"
    assert classify_category("Тепловой насос Daichi") == "heat_pumps"
    assert classify_category("Настенная сплит-система") == "air_conditioners"


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
