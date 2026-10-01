"""Перепроверяет ещё не вышедшие каталожные посты по текущим правилам отбора.

Правила отбора ужесточились (порог насыщенности, отбор фактов, очистка описаний), а
посты, поставленные в план раньше, остались со старым текстом. Скрипт для каждого
поста в статусах planned/review ищет позицию в снимке каталога:
  — позиции нет или она больше не проходит порог — пост снимается, слот займёт другой;
  — иначе текст пересобирается; изменился — пост возвращается на ревью с новым текстом.
Одобренные и уже отправленные в VK не трогаются. Идемпотентен.

    python scripts/refresh_catalog_posts.py [--dry-run]
"""
import re
import sys
from pathlib import Path

ROOT = Path("/opt/content-factory-vk")
sys.path.insert(0, str(ROOT / "src"))

from content_factory.orchestrator.vk_catalog_plan import (  # noqa: E402
    CATALOG_PREFIX, build_caption)
from content_factory.orchestrator.vk_content_plan import VkContentPlanStore  # noqa: E402
from content_factory.storefront.catalog_snapshot import load_snapshot  # noqa: E402
from content_factory.storefront.product_posts import is_postable  # noqa: E402

PRICE = re.compile(r"💎 ([0-9][0-9 ]*) ₽")
dry_run = "--dry-run" in sys.argv

store = VkContentPlanStore(str(ROOT / "state" / "vk-plan.db"))
catalog = {item.id: item for item in load_snapshot(ROOT / "state" / "catalog-snapshot.json")}
dropped = rewritten = kept = 0
for plan_item in store.list():
    if not plan_item.source_key.startswith(CATALOG_PREFIX):
        continue
    if plan_item.status not in {"planned", "review"}:
        continue
    item = catalog.get(plan_item.source_key[len(CATALOG_PREFIX):])
    if item is None or not is_postable(item):
        reason = "нет в снимке" if item is None else "слишком бедное описание"
        print(f"{plan_item.id}: снят ({reason}) — {plan_item.caption.splitlines()[2][:60]}")
        if not dry_run:
            store._transition(plan_item.id, ("planned", "review"), "superseded")
        dropped += 1
        continue
    price_match = PRICE.search(plan_item.caption)
    price = int(price_match.group(1).replace(" ", "")) if price_match else item.price
    fresh = build_caption(item, price)
    if fresh == plan_item.caption:
        kept += 1
        continue
    print(f"{plan_item.id}: текст обновлён — {item.name[:60]}")
    if not dry_run:
        store.update_caption(plan_item.id, fresh)
    rewritten += 1
print(f"итого: снято {dropped}, переписано {rewritten}, без изменений {kept}"
      + (" (пробный запуск, ничего не записано)" if dry_run else ""))
