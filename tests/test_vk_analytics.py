import sqlite3
from datetime import datetime
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from content_factory.analytics.vk import (
    Metric,
    Publication,
    VkAnalyticsStore,
    VkMetricsClient,
    attributed_counts,
    campaign_url,
    campaign_short_url,
    collect,
    editorial_destination,
    tracked_caption,
    weekly_report,
)
from content_factory.publish.orders import OrderLinks


def publication(plan_id=7, post_id=99, due_at=100):
    return Publication(plan_id, "product:1", post_id, due_at,
                       "air_conditioners", "product", campaign_url(plan_id))


def test_tracking_adds_unique_utm_and_attributed_order_link(tmp_path):
    links = OrderLinks(tmp_path / "source.db")
    text, url = tracked_caption(
        "Товар\n🌐 splithome.ru", 42, source_key="product:1",
        order_bot="OrderBot", links=links,
    )
    query = parse_qs(urlparse(url).query)
    assert query["utm_source"] == ["vk"]
    assert query["utm_content"] == ["vkp_42"]
    assert text.count("splithome.ru") == 0
    code = text.split("start=ord_", 1)[1]
    assert links.attribution_for(code) == ("product:1", "vk", "vkp_42")


def test_editorial_tracking_has_no_fake_order_link():
    text, tracked = tracked_caption(
        "Полезный пост\n\nИсточник: Завод — https://example.test/manual.pdf",
        9, source_key="editorial:one",
    )
    assert "utm_content=vkp_9" in tracked
    assert "Источник:" not in text
    assert "manual.pdf" not in text
    assert "splithome.ru" not in text
    assert "Заказать" not in text


def test_service_editorial_uses_direct_service_call_to_action():
    text, _ = tracked_caption(
        "Пора провести обслуживание", 13, source_key="editorial:service",
        editorial_destination="service",
    )
    assert "Записаться на обслуживание" in text
    assert campaign_short_url(13, intent="service") in text


def test_stabilizer_editorial_uses_one_compact_catalog_link():
    destination = editorial_destination("stabilizers", "useful")
    text, _ = tracked_caption(
        "Как выбрать стабилизатор\n\n1. Измерьте напряжение.", 26,
        source_key="editorial:stabilizer-measure-first",
        editorial_destination=destination,
    )
    assert "Смотреть стабилизаторы" in text
    assert campaign_short_url(26, intent="stabilizers") in text
    assert text.count("https://") == 1


def test_service_route_wins_over_catalog_category():
    assert editorial_destination("air_conditioners", "service") == "service"
    assert editorial_destination("air_conditioners", "useful") == "air_conditioners"


def test_metrics_client_collects_post_and_optional_reach(tmp_path):
    def handler(request):
        if request.url.path.endswith("wall.getById"):
            return httpx.Response(200, json={"response": [{
                "views": {"count": 120}, "likes": {"count": 5},
                "comments": {"count": 2}, "reposts": {"count": 1},
            }]})
        if request.url.path.endswith("stats.getPostReach"):
            return httpx.Response(200, json={"response": [{"reach_total": 80}]})
        raise AssertionError(request.url)

    store = VkAnalyticsStore(tmp_path / "plan.db")
    store.record_publication(publication(), now=101)
    client = VkMetricsClient(
        "TOKEN", -241020718,
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    result = collect(store, client, now=200)
    assert result == {"collected": 1, "missing": 0, "errors": []}
    assert store.weekly_totals(0, 300) == {
        "posts": 1, "views": 120, "reach": 80, "likes": 5,
        "comments": 2, "reposts": 1,
    }


def test_autonomy_defaults_l1_and_stops_after_three_failed_cycles(tmp_path):
    store = VkAnalyticsStore(tmp_path / "plan.db")
    assert store.level() == "L1"
    store.set_level("L2", now=100)
    assert not store.record_cycle(1, now=101)
    assert not store.record_cycle(1, now=102)
    assert store.record_cycle(1, now=103)
    assert store.level() == "L0"


def test_l3_requires_fourteen_clean_days(tmp_path):
    store = VkAnalyticsStore(tmp_path / "plan.db")
    with pytest.raises(ValueError, match="14 дней"):
        store.set_level("L3", now=15 * 86400)
    store.record_cycle(0, now=1)
    store.set_level("L3", now=15 * 86400)
    assert store.level() == "L3"


def test_weekly_report_includes_clicks_leads_and_conversion(tmp_path):
    plan_db, source_db = tmp_path / "plan.db", tmp_path / "source.db"
    store = VkAnalyticsStore(plan_db)
    now = datetime(2026, 8, 23, 12, 0)
    due = int(now.timestamp()) - 100
    store.record_publication(publication(due_at=due), now=due)
    store.add_metric(Metric(7, 200, None, 3, 1, 0), now=due + 1)
    links = OrderLinks(source_db)
    links.add_click(1, "a", "product:1", origin="vk", content_id="vkp_7")
    links.add_click(2, "b", "product:1", origin="vk", content_id="vkp_7")
    links.add_lead(1, "a", "product:1", origin="vk", content_id="vkp_7")
    with sqlite3.connect(source_db) as connection:
        connection.execute("UPDATE order_clicks SET ts=?", (due,))
        connection.execute("UPDATE leads SET ts=?", (due,))

    report = weekly_report(store, source_db, now=now)
    assert "Переходы в Telegram: 2" in report
    assert "заявки: 1" in report
    assert "конверсия: 50.0%" in report


def test_attributed_counts_handles_legacy_database(tmp_path):
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE leads(ts REAL,user_id INTEGER,username TEXT,key TEXT)")
    assert attributed_counts(path, 0, 10) == (0, 0)


def test_bootstrap_existing_planned_post_is_idempotent(tmp_path):
    path = tmp_path / "plan.db"
    store = VkAnalyticsStore(path)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE vk_content_plan (id INTEGER,source_key TEXT,vk_post_id INTEGER,"
            "due_at INTEGER,category TEXT,content_type TEXT)"
        )
        connection.execute(
            "INSERT INTO vk_content_plan VALUES(5,'product:5',77,100,'stabilizers','product')"
        )
    assert store.bootstrap_plan_publications(now=101) == 1
    assert store.bootstrap_plan_publications(now=102) == 0
    assert store.due_publications(200)[0].post_id == 77
