"""Четырнадцатидневный полуавтоматический план публикаций VK.

Пока VK не выдаёт издательскому токену загрузку фотографий, планировщик разделяет
одобрение текста, создание отложенной записи и подтверждение ручного фото. Все переходы
состояния идемпотентны и хранятся в отдельной SQLite-БД.
"""
from __future__ import annotations

import argparse
import hashlib
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

from content_factory.publish.telegram import publish_post, send_message
from content_factory.publish.vk import VkPublisher, adapt_vk_text
from content_factory.publish.vk_text_sync import (
    _published_rows,
    build_live_caption_map,
    build_vk_climate_text,
)
from content_factory.agents.editorial import build_editorial_drafts
from content_factory.analytics.vk import (
    LOW_RISK_TYPES,
    Publication,
    VkAnalyticsStore,
    tracked_caption,
)
from content_factory.publish.orders import OrderLinks
from content_factory.dedupe import post_fingerprint, text_similarity


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
    content_type: str = "product"


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
            connection.execute(
                "CREATE TABLE IF NOT EXISTS vk_dedupe_registry ("
                "dedupe_key TEXT PRIMARY KEY,source_key TEXT NOT NULL,"
                "content_fingerprint TEXT NOT NULL,normalized_text TEXT NOT NULL,"
                "content_type TEXT NOT NULL,created_at INTEGER NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS vk_publish_claims ("
                "dedupe_key TEXT PRIMARY KEY,plan_id INTEGER NOT NULL UNIQUE,claimed_at INTEGER NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS vk_dedupe_events ("
                "ts INTEGER NOT NULL,source_key TEXT NOT NULL,dedupe_key TEXT NOT NULL,"
                "kept_source_key TEXT NOT NULL,reason TEXT NOT NULL)"
            )
            self._bootstrap_dedupe(connection)

    def _connect(self):
        return sqlite3.connect(self.path)

    @staticmethod
    def _fingerprint(source_key: str, caption: str, category: str,
                     brand: str, content_type: str) -> tuple[str, str, str]:
        dedupe_key, normalized = post_fingerprint(
            source_key=source_key, caption=caption, category=category,
            brand=brand, content_type=content_type,
        )
        content_fingerprint = hashlib.sha256(
            normalized.encode("utf-8")
        ).hexdigest()
        return dedupe_key, content_fingerprint, normalized

    def _bootstrap_dedupe(self, connection: sqlite3.Connection) -> None:
        rows = connection.execute(
            "SELECT id,source_key,category,brand,content_type,caption,vk_post_id,created_at,status "
            "FROM vk_content_plan ORDER BY CASE status "
            "WHEN 'photo_confirmed' THEN 0 WHEN 'photo_pending' THEN 1 "
            "WHEN 'approved' THEN 2 WHEN 'review' THEN 3 WHEN 'planned' THEN 4 ELSE 5 END,id"
        ).fetchall()
        for (plan_id, source_key, category, brand, content_type, caption,
             post_id, created_at, status) in rows:
            if status not in ACTIVE_STATUSES and post_id is None:
                continue
            key, fingerprint, normalized = self._fingerprint(
                source_key, caption, category, brand, content_type,
            )
            near_source = self._near_duplicate_source(
                connection, normalized, content_type, exclude_source=source_key,
            )
            if near_source is not None:
                connection.execute(
                    "INSERT INTO vk_dedupe_events(ts,source_key,dedupe_key,kept_source_key,reason) "
                    "VALUES(?,?,?,?,?)",
                    (int(time.time()), source_key, key, near_source,
                     "existing_plan_near_duplicate"),
                )
                if status in {"planned", "review", "approved"}:
                    connection.execute(
                        "UPDATE vk_content_plan SET status='superseded_duplicate',updated_at=? "
                        "WHERE id=? AND status IN ('planned','review','approved')",
                        (int(time.time()), plan_id),
                    )
                continue
            cursor = connection.execute(
                "INSERT OR IGNORE INTO vk_dedupe_registry "
                "(dedupe_key,source_key,content_fingerprint,normalized_text,content_type,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (key, source_key, fingerprint, normalized, content_type, created_at),
            )
            if not cursor.rowcount:
                kept = connection.execute(
                    "SELECT source_key FROM vk_dedupe_registry WHERE dedupe_key=?",
                    (key,),
                ).fetchone()
                kept_source = str(kept[0]) if kept else "unknown"
                if kept_source != source_key:
                    connection.execute(
                        "INSERT INTO vk_dedupe_events(ts,source_key,dedupe_key,kept_source_key,reason) "
                        "VALUES(?,?,?,?,?)",
                        (int(time.time()), source_key, key, kept_source,
                         "existing_plan_duplicate"),
                    )
                    if status in {"planned", "review", "approved"}:
                        connection.execute(
                            "UPDATE vk_content_plan SET status='superseded_duplicate',updated_at=? "
                            "WHERE id=? AND status IN ('planned','review','approved')",
                            (int(time.time()), plan_id),
                        )
            if post_id is not None:
                connection.execute(
                    "INSERT OR IGNORE INTO vk_publish_claims(dedupe_key,plan_id,claimed_at) "
                    "VALUES(?,?,?)", (key, plan_id, created_at),
                )

    @staticmethod
    def _near_duplicate_source(connection: sqlite3.Connection, normalized: str,
                               content_type: str,
                               threshold: float = 0.88,
                               exclude_source: str = "") -> str | None:
        if content_type == "product" or not normalized:
            return None
        rows = connection.execute(
            "SELECT source_key,normalized_text FROM vk_dedupe_registry WHERE content_type=?",
            (content_type,),
        ).fetchall()
        return next((str(source_key) for source_key, existing in rows
                     if str(source_key) != str(exclude_source)
                     and text_similarity(normalized, existing) >= threshold), None)

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
            if connection.execute(
                "SELECT 1 FROM vk_content_plan WHERE source_key=?", (candidate.source_key,),
            ).fetchone():
                return None
            key, fingerprint, normalized = self._fingerprint(
                candidate.source_key, candidate.caption, candidate.category,
                candidate.brand, candidate.content_type,
            )
            near_source = self._near_duplicate_source(
                connection, normalized, candidate.content_type,
            )
            if connection.execute(
                "SELECT 1 FROM vk_dedupe_registry WHERE dedupe_key=? OR content_fingerprint=?",
                (key, fingerprint),
            ).fetchone() or near_source is not None:
                kept = connection.execute(
                    "SELECT source_key,dedupe_key FROM vk_dedupe_registry "
                    "WHERE dedupe_key=? OR content_fingerprint=? LIMIT 1",
                    (key, fingerprint),
                ).fetchone()
                connection.execute(
                    "INSERT INTO vk_dedupe_events(ts,source_key,dedupe_key,kept_source_key,reason) "
                    "VALUES(?,?,?,?,?)",
                    (now, candidate.source_key, key,
                     str(kept[0]) if kept else str(near_source or "near_match"),
                     "exact_or_near_duplicate"),
                )
                return None
            connection.execute(
                "INSERT INTO vk_dedupe_registry "
                "(dedupe_key,source_key,content_fingerprint,normalized_text,content_type,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (key, candidate.source_key, fingerprint, normalized,
                 candidate.content_type, now),
            )
            cursor = connection.execute(
                "INSERT INTO vk_content_plan "
                "(source_key,due_at,category,brand,content_type,caption,card_path,status,"
                "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (candidate.source_key, int(due_at), candidate.category, candidate.brand,
                 candidate.content_type, candidate.caption, candidate.card_path, "planned", now, now),
            )
            return int(cursor.lastrowid)

    def claim_publication(self, item_id: int) -> bool:
        item = self.get(item_id)
        if item is None:
            return False
        key, _fingerprint, _normalized = self._fingerprint(
            item.source_key, item.caption, item.category, item.brand, item.content_type,
        )
        with self._connect() as connection:
            cursor = connection.execute(
                "INSERT OR IGNORE INTO vk_publish_claims(dedupe_key,plan_id,claimed_at) "
                "VALUES(?,?,?)", (key, item.id, int(time.time())),
            )
        return bool(cursor.rowcount)

    def release_publication_claim(self, item_id: int) -> None:
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM vk_publish_claims WHERE plan_id=?", (int(item_id),),
            )

    def dedupe_counts(self) -> dict[str, int]:
        with self._connect() as connection:
            registry = connection.execute("SELECT COUNT(*) FROM vk_dedupe_registry").fetchone()[0]
            claims = connection.execute("SELECT COUNT(*) FROM vk_publish_claims").fetchone()[0]
            blocked = connection.execute("SELECT COUNT(*) FROM vk_dedupe_events").fetchone()[0]
        return {"registry": int(registry), "published_claims": int(claims),
                "blocked": int(blocked)}

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
                "WHERE content_type='product' AND status IN ('planned','review','approved')"
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

    def auto_approve(self, content_types: tuple[str, ...] | None = None) -> list[int]:
        """Одобрить только ещё не показанные материалы разрешённых типов."""
        candidates = [item for item in self.list() if item.status == "planned"]
        if content_types is not None:
            candidates = [item for item in candidates if item.content_type in content_types]
        approved = []
        for item in candidates:
            if self._transition(item.id, ("planned",), "approved"):
                approved.append(item.id)
        return approved

    def reject(self, item_id: int) -> bool:
        return self._transition(item_id, ("review", "approved"), "rejected")

    def mark_scheduled(self, item_id: int, post_id: int) -> bool:
        return self._transition(item_id, ("approved",), "photo_pending", vk_post_id=post_id)

    def confirm_photo(self, item_id: int) -> bool:
        return self._transition(item_id, ("photo_pending",), "photo_confirmed")

    def mark_reminded(self, item_id: int) -> bool:
        return self._transition(item_id, ("photo_pending",), "photo_pending",
                                reminder_sent_at=int(time.time()))

    def rebalance_products(self, max_products: int = 3, now: int | None = None) -> int:
        """Оставить квоту товарных слотов, не трогая уже показанные владельцу записи."""
        active = [item for item in self.list()
                  if item.content_type == "product" and item.status in ACTIVE_STATUSES
                  and (now is None or item.due_at > int(now))]
        remove_count = max(0, len(active) - int(max_products))
        removable = [item for item in reversed(active) if item.status == "planned"]
        changed = 0
        for item in removable[:remove_count]:
            changed += int(self._transition(item.id, ("planned",), "superseded"))
        return changed

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


def item_code(item: VkPlanItem | int) -> str:
    """Короткий неизменяемый код материала для Telegram и журнала."""
    item_id = item.id if isinstance(item, VkPlanItem) else int(item)
    return f"CF-VK-{item_id:03d}"


def item_title(item: VkPlanItem, limit: int = 64) -> str:
    """Человекочитаемая модель из первой строки без служебных символов."""
    title = next((line.strip() for line in (item.caption or "").splitlines()
                  if line.strip()), item.source_key)
    title = re.sub(r"^[^A-Za-zА-Яа-яЁё0-9]+", "", title).strip()
    if len(title) <= limit:
        return title
    return title[:max(1, limit - 1)].rstrip() + "…"


def item_reference(item: VkPlanItem) -> str:
    return f"{item_code(item)} · {item_title(item)}"


def callback_markup(item: VkPlanItem | int) -> str:
    code = item_code(item)
    item_id = item.id if isinstance(item, VkPlanItem) else int(item)
    return json.dumps({"inline_keyboard": [[
        {"text": f"✅ Одобрить {code}", "callback_data": f"vkp:a:{item_id}"},
        {"text": f"❌ Пропустить {code}", "callback_data": f"vkp:r:{item_id}"},
    ]]}, ensure_ascii=False)


def photo_markup(item: VkPlanItem | int) -> str:
    code = item_code(item)
    item_id = item.id if isinstance(item, VkPlanItem) else int(item)
    return json.dumps({"inline_keyboard": [[
        {"text": f"📷 Фото прикреплено · {code}", "callback_data": f"vkp:p:{item_id}"},
    ]]}, ensure_ascii=False)


def handle_plan_callback(data: str, store: VkContentPlanStore) -> str:
    match = re.fullmatch(r"vkp:([arp]):(\d+)", data or "")
    if not match:
        return "Неизвестное действие VK-плана"
    action, raw_id = match.groups()
    item_id = int(raw_id)
    item = store.get(item_id)
    if item is None:
        return "Материал VK-плана не найден"
    reference = item_reference(item)
    if action == "a":
        return (f"✅ {reference} одобрен. Планировщик создаст отложенную запись."
                if store.approve(item_id) else "Материал уже обработан")
    if action == "r":
        return (f"❌ {reference} исключён из плана."
                if store.reject(item_id) else "Материал уже обработан")
    return (f"📷 {reference}: фото отмечено как прикреплённое."
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
                     now: datetime, horizon_days: int = 14,
                     max_products: int = 3) -> list[int]:
    store.synchronize_candidates(candidates)
    store.rebalance_products(max_products, int(now.timestamp()))
    slots = plan_slots(now, horizon_days=horizon_days)
    occupied = {item.due_at for item in store.list() if item.status in ACTIVE_STATUSES}
    free_slots = [slot for slot in slots if slot not in occupied]
    existing = store.source_keys()
    current_products = sum(
        item.content_type == "product" and item.status in ACTIVE_STATUSES
        and item.due_at > int(now.timestamp())
        for item in store.list()
    )
    selected = choose_candidates(
        [candidate for candidate in candidates if candidate.source_key not in existing],
        min(len(free_slots), max(0, int(max_products) - current_products)),
    )
    added: list[int] = []
    for candidate, due_at in zip(selected, free_slots):
        item_id = store.add(candidate, due_at)
        if item_id is not None:
            added.append(item_id)
    return added


def materialize_editorial_plan(store: VkContentPlanStore, knowledge_path: str | Path,
                               now: datetime, horizon_days: int = 14) -> list[int]:
    slots = plan_slots(now, horizon_days=horizon_days)
    occupied = {item.due_at for item in store.list() if item.status in ACTIVE_STATUSES}
    free_slots = [slot for slot in slots if slot not in occupied]
    cutoff = int((now - timedelta(days=14)).timestamp())
    used = {item.source_key.split(":")[1] for item in store.list()
            if item.source_key.startswith("editorial:") and item.due_at >= cutoff}
    drafts = build_editorial_drafts(
        knowledge_path, used, len(free_slots), audit_db=store.path,
    )
    added = []
    for draft, due_at in zip(drafts, free_slots):
        item_id = store.add(VkPlanCandidate(
            source_key=f"editorial:{draft.idea_id}:{datetime.fromtimestamp(due_at):%Y%m%d}",
            source_ts=float(due_at),
            caption=draft.text, card_path="", category=draft.category,
            brand="EDITORIAL", content_type=draft.content_type,
        ), due_at)
        if item_id is not None:
            added.append(item_id)
    return added


def review_caption(item: VkPlanItem, limit: int = 1024, *, body: str | None = None) -> str:
    due = datetime.fromtimestamp(item.due_at).strftime("%d.%m.%Y %H:%M")
    header = f"🆔 {item_reference(item)}\n🗓 VK · {due} · {item.category}\n\n"
    room = int(limit) - len(header)
    return header + (body if body is not None else item.caption)[:room].rstrip()


def photo_task_caption(item: VkPlanItem, *, reminder: bool = False) -> str:
    """Подпись к точной карточке, которую нужно приложить к записи VK."""
    due = datetime.fromtimestamp(item.due_at).strftime("%d.%m %H:%M")
    prefix = "⚠️ Напоминание\n" if reminder else "📌 Задание на фотографию\n"
    return (
        f"{prefix}"
        f"🆔 {item_reference(item)}\n"
        f"🧱 Отложенная запись VK №{item.vk_post_id}\n"
        f"🗓 Публикация: {due}\n\n"
        "Прикрепите именно эту карточку к указанной записи VK, затем подтвердите кнопкой ниже."
    )


def send_text_with_markup(token: str, chat_id: str, text: str, markup: str,
                          http: httpx.Client) -> int | None:
    try:
        response = http.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={"chat_id": str(chat_id), "text": text, "reply_markup": markup},
        )
        payload = response.json() or {}
        return int((payload.get("result") or {}).get("message_id")) if payload.get("ok") else None
    except (httpx.HTTPError, ValueError):
        return None


def run_cycle(*, store: VkContentPlanStore, source_db: str, telegram_token: str,
              review_chat: str, vk_token: str, owner_id: int, now: datetime,
              dry_run: bool = True, http: httpx.Client | None = None,
              editorial_knowledge: str | Path = "config/vk-editorial-sources.yaml",
              native_photo_enabled: bool = False, autonomy_level: str = "L1",
              analytics_store: VkAnalyticsStore | None = None,
              order_links: OrderLinks | None = None, order_bot: str = "Sendpr1ce_bot",
              site_url: str = "https://splithome.ru/") -> dict:
    client = http or httpx.Client(timeout=60)
    result = {"planned": [], "auto_approved": [], "reviewed": [], "scheduled": [],
              "reminded": [], "errors": [], "autonomy_level": autonomy_level}

    try:
        live_captions = build_live_caption_map()
        candidates = load_candidates(source_db, live_captions)
        result["planned"] = materialize_plan(store, candidates, now)
        if Path(editorial_knowledge).is_file():
            result["planned"].extend(materialize_editorial_plan(
                store, editorial_knowledge, now,
            ))
    except Exception as exc:  # каталог недоступен: не планируем материал со старой ценой
        result["errors"].append(f"plan_failed: {exc}")

    if autonomy_level == "L2":
        result["auto_approved"] = store.auto_approve(LOW_RISK_TYPES)
    elif autonomy_level == "L3":
        result["auto_approved"] = store.auto_approve()

    def publication_caption(item: VkPlanItem) -> tuple[str, str]:
        return tracked_caption(
            item.caption, item.id, source_key=item.source_key,
            order_bot=order_bot, links=order_links, base_url=site_url,
        )

    for item in store.for_review(int(now.timestamp())):
        body, _tracked_url = publication_caption(item)
        if dry_run:
            result["reviewed"].append(item.id)
            continue
        if item.card_path:
            preview = publish_post(
                telegram_token, review_chat, item.card_path, review_caption(item, body=body),
                http=client, reply_markup=callback_markup(item), retries=1,
            )
            message_id = preview.message_id if preview.ok else None
            error = preview.error
        else:
            message_id = send_text_with_markup(
                telegram_token, review_chat, review_caption(item, 4000, body=body),
                callback_markup(item), client,
            )
            error = None if message_id else "Telegram sendMessage failed"
        if message_id and store.mark_review(item.id, message_id):
            result["reviewed"].append(item.id)
        else:
            result["errors"].append(f"review {item.id}: {error or 'state conflict'}")

    publisher = VkPublisher(vk_token, owner_id, dry_run=dry_run, http=client)
    for item in store.approved(int(now.timestamp())):
        body, tracked_url = publication_caption(item)
        native_photo = bool(native_photo_enabled and item.card_path)
        if not dry_run and not store.claim_publication(item.id):
            result["errors"].append(
                f"schedule {item.id}: duplicate publication fingerprint blocked"
            )
            continue
        scheduled = (
            publisher.publish(item.card_path, body, publish_at=item.due_at)
            if native_photo else
            publisher.publish_text(body, publish_at=item.due_at)
        )
        if scheduled.ok and scheduled.post_id is not None and not scheduled.dry_run:
            if store.mark_scheduled(item.id, scheduled.post_id):
                result["scheduled"].append(item.id)
                if analytics_store is not None:
                    analytics_store.record_publication(Publication(
                        plan_id=item.id, source_key=item.source_key,
                        post_id=scheduled.post_id, due_at=item.due_at,
                        category=item.category, content_type=item.content_type,
                        tracked_url=tracked_url,
                    ))
                due = datetime.fromtimestamp(item.due_at).strftime("%d.%m %H:%M")
                if native_photo:
                    store.confirm_photo(item.id)
                    send_message(
                        telegram_token, review_chat,
                        f"VK-пост №{scheduled.post_id} с нативной фотографией запланирован на {due}.",
                        http=client,
                    )
                elif item.card_path:
                    photo_task = publish_post(
                        telegram_token, review_chat, item.card_path,
                        photo_task_caption(store.get(item.id) or item),
                        http=client, reply_markup=photo_markup(item), retries=1,
                    )
                    if not photo_task.ok:
                        result["errors"].append(
                            f"photo task {item.id}: {photo_task.error or 'Telegram sendPhoto failed'}"
                        )
                else:
                    store.confirm_photo(item.id)
                    send_message(
                        telegram_token, review_chat,
                        f"VK-пост №{scheduled.post_id} запланирован на {due}. "
                        "Это редакционный текст без товарной фотографии.",
                        http=client,
                    )
        elif not scheduled.ok:
            if not dry_run:
                store.release_publication_claim(item.id)
            result["errors"].append(
                f"schedule {item.id}: {scheduled.error or 'VK did not accept publication'}"
            )

    for item in store.reminders(int(now.timestamp())):
        if dry_run:
            result["reminded"].append(item.id)
            continue
        reminder = publish_post(
            telegram_token, review_chat, item.card_path,
            photo_task_caption(item, reminder=True),
            http=client, reply_markup=photo_markup(item), retries=1,
        )
        if reminder.ok and store.mark_reminded(item.id):
            result["reminded"].append(item.id)
        elif not reminder.ok:
            result["errors"].append(
                f"reminder {item.id}: {reminder.error or 'Telegram sendPhoto failed'}"
            )
    result["dedupe"] = store.dedupe_counts()
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="VK Content Planner v1")
    parser.add_argument("--source-db", default=os.getenv("CF_SOURCE_DB", DEFAULT_SOURCE_DB))
    parser.add_argument("--state-db", default=os.getenv("VK_PLAN_STATE_DB", DEFAULT_PLAN_DB))
    parser.add_argument("--owner-id", type=int, default=int(os.getenv("VK_OWNER_ID", "-241020718")))
    parser.add_argument("--publish", action="store_true", help="отправлять review и создавать отложенные записи")
    args = parser.parse_args(argv)
    publish_enabled = args.publish or os.getenv("VK_PLAN_PUBLISH", "0") == "1"
    analytics = VkAnalyticsStore(args.state_db)
    autonomy_level = analytics.level()
    source_db = str(args.source_db)
    result = run_cycle(
        store=VkContentPlanStore(args.state_db), source_db=source_db,
        telegram_token=config("TELEGRAM_BOT_TOKEN", default=""),
        review_chat=config("TELEGRAM_REVIEW_CHANNEL_ID", default=""),
        vk_token=config("VK_ACCESS_TOKEN", default=""), owner_id=args.owner_id,
        now=datetime.now(), dry_run=(not publish_enabled or autonomy_level == "L0"),
        native_photo_enabled=os.getenv("VK_NATIVE_PHOTO_ENABLED", "0") == "1",
        autonomy_level=autonomy_level, analytics_store=analytics,
        order_links=OrderLinks(source_db),
        order_bot=os.getenv("VK_ORDER_BOT", "Sendpr1ce_bot"),
        site_url=os.getenv("VK_SITE_URL", "https://splithome.ru/"),
    )
    auto_stopped = analytics.record_cycle(len(result["errors"]))
    result["auto_stopped"] = auto_stopped
    if auto_stopped:
        send_message(
            config("TELEGRAM_BOT_TOKEN", default=""),
            config("TELEGRAM_REVIEW_CHANNEL_ID", default=""),
            "⛔ VK-контент-завод автоматически переведён в L0 после трёх "
            "ошибочных циклов подряд. Публикации остановлены до проверки.",
        )
    print(json.dumps(result, ensure_ascii=False))
    return 1 if result["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
