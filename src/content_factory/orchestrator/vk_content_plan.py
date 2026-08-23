"""Четырнадцатидневный полуавтоматический план публикаций VK.

Пока VK не выдаёт издательскому токену загрузку фотографий, планировщик разделяет
одобрение текста, создание отложенной записи и подтверждение ручного фото. Все переходы
состояния идемпотентны и хранятся в отдельной SQLite-БД.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import httpx
from decouple import config

from content_factory.publish.telegram import publish_post
from content_factory.publish.vk import VkPublisher, adapt_vk_text
from content_factory.publish.vk_text_sync import (
    _published_rows,
    build_live_caption_map,
    build_vk_climate_text,
)


DEFAULT_PLAN_DB = "/opt/content-factory-vk/state/vk-plan.db"
DEFAULT_SOURCE_DB = "/opt/content-factory/state/content_factory.db"
ACTIVE_STATUSES = {"planned", "review", "approved", "photo_pending", "photo_confirmed"}


@dataclass(frozen=True)
class VkPlanCandidate:
    source_key: str
    source_ts: float
    caption: str
    card_path: str
    category: str
    brand: str


@dataclass(frozen=True)
class VkPlanItem:
    id: int
    source_key: str
    due_at: int
    category: str
    brand: str
    content_type: str
    caption: str
    card_path: str
    status: str
    telegram_message_id: int | None
    vk_post_id: int | None
    reminder_sent_at: int | None


def classify_category(text: str) -> str:
    value = adapt_vk_text(text or "").casefold()
    checks = (
        ("stabilizers", ("стабилизатор",)),
        ("ups", ("источник бесперебой", "ибп", " ups")),
        ("recuperators", ("рекуператор",)),
        ("ventilation", ("вентиляц", "приточн", "вытяжн")),
        ("heat_pumps", ("тепловой насос", "тепловые насос")),
        ("air_conditioners", ("кондиционер", "сплит-систем", "сплит систем")),
    )
    for category, terms in checks:
        if any(term in value for term in terms):
            return category
    if "btu" in value and ("м²" in value or "м2" in value):
        return "air_conditioners"
    return "climate"


def extract_brand(caption: str) -> str:
    first = next((line.strip() for line in adapt_vk_text(caption).splitlines() if line.strip()), "")
    first = re.sub(r"^[^A-Za-zА-Яа-яЁё0-9]+", "", first)
    words = first.split()
    return words[0].upper() if words else "UNKNOWN"


def plan_slots(start: datetime, *, horizon_days: int = 14,
               times: tuple[str, ...] = ("11:30", "18:30"),
               weekdays: tuple[int, ...] = (0, 1, 2, 3, 4, 5)) -> list[int]:
    """Один слот в день, понедельник–суббота, время чередуется."""
    slots: list[int] = []
    day = start.date()
    end = day + timedelta(days=horizon_days)
    index = 0
    while day < end:
        if day.weekday() in weekdays:
            hour, minute = map(int, times[index % len(times)].split(":"))
            due = datetime.combine(day, datetime.min.time()).replace(hour=hour, minute=minute)
            if due > start:
                slots.append(int(due.timestamp()))
                index += 1
        day += timedelta(days=1)
    return slots


def choose_candidates(candidates: list[VkPlanCandidate], count: int) -> list[VkPlanCandidate]:
    """Детерминированная ротация без одинаковой категории/бренда подряд."""
    pool = sorted(candidates, key=lambda item: (-item.source_ts, item.source_key))
    chosen: list[VkPlanCandidate] = []
    while pool and len(chosen) < count:
        previous = chosen[-1] if chosen else None
        index = next((
            i for i, candidate in enumerate(pool)
            if previous is None
            or (candidate.category != previous.category and candidate.brand != previous.brand)
        ), None)
        if index is None:
            index = next((
                i for i, candidate in enumerate(pool)
                if previous is None or candidate.category != previous.category
            ), 0)
        chosen.append(pool.pop(index))
    return chosen


class VkContentPlanStore:
    def __init__(self, path: str | Path = DEFAULT_PLAN_DB):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS vk_content_plan ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "source_key TEXT NOT NULL UNIQUE, due_at INTEGER NOT NULL, "
                "category TEXT NOT NULL, brand TEXT NOT NULL, "
                "content_type TEXT NOT NULL DEFAULT 'product', "
                "caption TEXT NOT NULL, card_path TEXT NOT NULL, "
                "status TEXT NOT NULL DEFAULT 'planned', "
                "telegram_message_id INTEGER, vk_post_id INTEGER, "
                "reminder_sent_at INTEGER, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL)"
            )

    def _connect(self):
        return sqlite3.connect(self.path)

    @staticmethod
    def _item(row) -> VkPlanItem:
        return VkPlanItem(*row)

    def source_keys(self) -> set[str]:
        with self._connect() as connection:
            return {row[0] for row in connection.execute(
                "SELECT source_key FROM vk_content_plan"
            )}

    def add(self, candidate: VkPlanCandidate, due_at: int) -> int | None:
        now = int(time.time())
        with self._connect() as connection:
            cursor = connection.execute(
                "INSERT OR IGNORE INTO vk_content_plan "
                "(source_key,due_at,category,brand,content_type,caption,card_path,status,"
                "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (candidate.source_key, int(due_at), candidate.category, candidate.brand,
                 "product", candidate.caption, candidate.card_path, "planned", now, now),
            )
            return int(cursor.lastrowid) if cursor.rowcount else None

    def synchronize_candidates(self, candidates: list[VkPlanCandidate]) -> dict[str, int]:
        """Обновить ещё не отправленные позиции и снять исчезнувшие из наличия.

        Если уже показанный или одобренный текст изменился, он возвращается на ревью:
        владелец никогда не подтверждает одну цену, а публикует другую молча.
        """
        by_key = {candidate.source_key: candidate for candidate in candidates}
        now = int(time.time())
        changed = 0
        blocked = 0
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT source_key,caption,card_path,status FROM vk_content_plan "
                "WHERE status IN ('planned','review','approved')"
            ).fetchall()
            for source_key, caption, card_path, status in rows:
                candidate = by_key.get(str(source_key))
                if candidate is None:
                    cursor = connection.execute(
                        "UPDATE vk_content_plan SET status='blocked_unavailable',updated_at=? "
                        "WHERE source_key=? AND status IN ('planned','review','approved')",
                        (now, source_key),
                    )
                    blocked += int(cursor.rowcount)
                    continue
                content_changed = candidate.caption != caption or candidate.card_path != card_path
                next_status = "planned" if content_changed and status != "planned" else status
                cursor = connection.execute(
                    "UPDATE vk_content_plan SET category=?,brand=?,caption=?,card_path=?,"
                    "status=?,telegram_message_id=CASE WHEN ?='planned' AND status!='planned' "
                    "THEN NULL ELSE telegram_message_id END,updated_at=? WHERE source_key=?",
                    (candidate.category, candidate.brand, candidate.caption, candidate.card_path,
                     next_status, next_status, now, source_key),
                )
                changed += int(cursor.rowcount and (
                    content_changed or candidate.category != classify_category(caption)
                ))
        return {"changed": changed, "blocked": blocked}

    def list(self) -> list[VkPlanItem]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT id,source_key,due_at,category,brand,content_type,caption,card_path,"
                "status,telegram_message_id,vk_post_id,reminder_sent_at "
                "FROM vk_content_plan ORDER BY due_at"
            ).fetchall()
        return [self._item(row) for row in rows]

    def get(self, item_id: int) -> VkPlanItem | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT id,source_key,due_at,category,brand,content_type,caption,card_path,"
                "status,telegram_message_id,vk_post_id,reminder_sent_at "
                "FROM vk_content_plan WHERE id=?", (int(item_id),)
            ).fetchone()
        return self._item(row) if row else None

    def _transition(self, item_id: int, allowed: tuple[str, ...], status: str,
                    **values) -> bool:
        assignments = ["status=?", "updated_at=?"]
        args: list[object] = [status, int(time.time())]
        for key, value in values.items():
            if key not in {"telegram_message_id", "vk_post_id", "reminder_sent_at"}:
                raise ValueError(f"Недопустимое поле плана: {key}")
            assignments.append(f"{key}=?")
            args.append(value)
        placeholders = ",".join("?" for _ in allowed)
        args.extend([int(item_id), *allowed])
        with self._connect() as connection:
            cursor = connection.execute(
                f"UPDATE vk_content_plan SET {', '.join(assignments)} "
                f"WHERE id=? AND status IN ({placeholders})", args,
            )
            return bool(cursor.rowcount)

    def mark_review(self, item_id: int, message_id: int | None) -> bool:
        return self._transition(item_id, ("planned",), "review",
                                telegram_message_id=message_id)

    def approve(self, item_id: int) -> bool:
        return self._transition(item_id, ("review",), "approved")

    def reject(self, item_id: int) -> bool:
        return self._transition(item_id, ("review", "approved"), "rejected")

    def mark_scheduled(self, item_id: int, post_id: int) -> bool:
        return self._transition(item_id, ("approved",), "photo_pending", vk_post_id=post_id)

    def confirm_photo(self, item_id: int) -> bool:
        return self._transition(item_id, ("photo_pending",), "photo_confirmed")

    def mark_reminded(self, item_id: int) -> bool:
        return self._transition(item_id, ("photo_pending",), "photo_pending",
                                reminder_sent_at=int(time.time()))

    def for_review(self, now: int, lead_hours: int = 48, limit: int = 1) -> list[VkPlanItem]:
        upper = int(now) + int(lead_hours) * 3600
        return [item for item in self.list()
                if item.status == "planned" and int(now) < item.due_at <= upper][:limit]

    def approved(self, now: int | None = None, lead_hours: int = 24) -> list[VkPlanItem]:
        upper = None if now is None else int(now) + int(lead_hours) * 3600
        return [item for item in self.list()
                if item.status == "approved"
                and (upper is None or int(now) < item.due_at <= upper)]

    def reminders(self, now: int, hours_before: int = 3) -> list[VkPlanItem]:
        upper = int(now) + int(hours_before) * 3600
        return [item for item in self.list()
                if item.status == "photo_pending" and item.reminder_sent_at is None
                and int(now) < item.due_at <= upper]


def callback_markup(item_id: int) -> str:
    return json.dumps({"inline_keyboard": [[
        {"text": "✅ Одобрить для VK", "callback_data": f"vkp:a:{item_id}"},
        {"text": "❌ Пропустить", "callback_data": f"vkp:r:{item_id}"},
    ]]}, ensure_ascii=False)


def photo_markup(item_id: int) -> str:
    return json.dumps({"inline_keyboard": [[
        {"text": "📷 Фото прикреплено", "callback_data": f"vkp:p:{item_id}"},
    ]]}, ensure_ascii=False)


def handle_plan_callback(data: str, store: VkContentPlanStore) -> str:
    match = re.fullmatch(r"vkp:([arp]):(\d+)", data or "")
    if not match:
        return "Неизвестное действие VK-плана"
    action, raw_id = match.groups()
    item_id = int(raw_id)
    if store.get(item_id) is None:
        return "Материал VK-плана не найден"
    if action == "a":
        return ("✅ Материал одобрен. Планировщик создаст отложенную запись."
                if store.approve(item_id) else "Материал уже обработан")
    if action == "r":
        return "❌ Материал исключён из плана." if store.reject(item_id) else "Материал уже обработан"
    return ("📷 Фото отмечено как прикреплённое."
            if store.confirm_photo(item_id) else "Фото уже подтверждено или запись ещё не создана")


def load_candidates(source_db: str | Path, live_captions: dict[str, str]) -> list[VkPlanCandidate]:
    candidates: list[VkPlanCandidate] = []
    for key, source_ts, _old_caption, card_path in _published_rows(source_db):
        fresh = live_captions.get(str(key))
        if not fresh or not card_path or not Path(card_path).is_file():
            continue
        caption = build_vk_climate_text(fresh)
        candidates.append(VkPlanCandidate(
            source_key=str(key), source_ts=float(source_ts or 0), caption=caption,
            card_path=str(card_path), category=classify_category(caption),
            brand=extract_brand(caption),
        ))
    return candidates


def materialize_plan(store: VkContentPlanStore, candidates: list[VkPlanCandidate],
                     now: datetime, horizon_days: int = 14) -> list[int]:
    store.synchronize_candidates(candidates)
    slots = plan_slots(now, horizon_days=horizon_days)
    occupied = {item.due_at for item in store.list() if item.status in ACTIVE_STATUSES}
    free_slots = [slot for slot in slots if slot not in occupied]
    existing = store.source_keys()
    selected = choose_candidates(
        [candidate for candidate in candidates if candidate.source_key not in existing],
        len(free_slots),
    )
    added: list[int] = []
    for candidate, due_at in zip(selected, free_slots):
        item_id = store.add(candidate, due_at)
        if item_id is not None:
            added.append(item_id)
    return added


def review_caption(item: VkPlanItem) -> str:
    due = datetime.fromtimestamp(item.due_at).strftime("%d.%m.%Y %H:%M")
    header = f"VK · {due} · {item.category}\n\n"
    room = 1024 - len(header)
    return header + item.caption[:room].rstrip()


def send_text_with_markup(token: str, chat_id: str, text: str, markup: str,
                          http: httpx.Client) -> bool:
    try:
        response = http.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={"chat_id": str(chat_id), "text": text, "reply_markup": markup},
        )
        return bool((response.json() or {}).get("ok"))
    except (httpx.HTTPError, ValueError):
        return False


def run_cycle(*, store: VkContentPlanStore, source_db: str, telegram_token: str,
              review_chat: str, vk_token: str, owner_id: int, now: datetime,
              dry_run: bool = True, http: httpx.Client | None = None) -> dict:
    client = http or httpx.Client(timeout=60)
    result = {"planned": [], "reviewed": [], "scheduled": [], "reminded": [], "errors": []}

    try:
        live_captions = build_live_caption_map()
        candidates = load_candidates(source_db, live_captions)
        result["planned"] = materialize_plan(store, candidates, now)
    except Exception as exc:  # каталог недоступен: не планируем материал со старой ценой
        result["errors"].append(f"plan_failed: {exc}")

    for item in store.for_review(int(now.timestamp())):
        if dry_run:
            result["reviewed"].append(item.id)
            continue
        preview = publish_post(
            telegram_token, review_chat, item.card_path, review_caption(item),
            http=client, reply_markup=callback_markup(item.id), retries=1,
        )
        if preview.ok and store.mark_review(item.id, preview.message_id):
            result["reviewed"].append(item.id)
        else:
            result["errors"].append(f"review {item.id}: {preview.error or 'state conflict'}")

    publisher = VkPublisher(vk_token, owner_id, dry_run=dry_run, http=client)
    for item in store.approved(int(now.timestamp())):
        scheduled = publisher.publish_text(item.caption, publish_at=item.due_at)
        if scheduled.ok and scheduled.post_id is not None and not scheduled.dry_run:
            if store.mark_scheduled(item.id, scheduled.post_id):
                result["scheduled"].append(item.id)
                due = datetime.fromtimestamp(item.due_at).strftime("%d.%m %H:%M")
                send_text_with_markup(
                    telegram_token, review_chat,
                    f"VK-пост №{scheduled.post_id} запланирован на {due}. "
                    "Прикрепите карточку к отложенной записи и подтвердите кнопкой.",
                    photo_markup(item.id), client,
                )
        elif scheduled.error:
            result["errors"].append(f"schedule {item.id}: {scheduled.error}")

    for item in store.reminders(int(now.timestamp())):
        if dry_run:
            result["reminded"].append(item.id)
            continue
        due = datetime.fromtimestamp(item.due_at).strftime("%d.%m %H:%M")
        ok = send_text_with_markup(
            telegram_token, review_chat,
            f"⚠️ До публикации VK №{item.vk_post_id} ({due}) осталось меньше трёх часов, "
            "а фото ещё не подтверждено. Прикрепите карточку или вручную отмените запись.",
            photo_markup(item.id), client,
        )
        if ok and store.mark_reminded(item.id):
            result["reminded"].append(item.id)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="VK Content Planner v1")
    parser.add_argument("--source-db", default=os.getenv("CF_SOURCE_DB", DEFAULT_SOURCE_DB))
    parser.add_argument("--state-db", default=os.getenv("VK_PLAN_STATE_DB", DEFAULT_PLAN_DB))
    parser.add_argument("--owner-id", type=int, default=int(os.getenv("VK_OWNER_ID", "-241020718")))
    parser.add_argument("--publish", action="store_true", help="отправлять review и создавать отложенные записи")
    args = parser.parse_args(argv)
    publish_enabled = args.publish or os.getenv("VK_PLAN_PUBLISH", "0") == "1"
    result = run_cycle(
        store=VkContentPlanStore(args.state_db), source_db=args.source_db,
        telegram_token=config("TELEGRAM_BOT_TOKEN", default=""),
        review_chat=config("TELEGRAM_REVIEW_CHANNEL_ID", default=""),
        vk_token=config("VK_ACCESS_TOKEN", default=""), owner_id=args.owner_id,
        now=datetime.now(), dry_run=not publish_enabled,
    )
    print(json.dumps(result, ensure_ascii=False))
    return 1 if result["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
