"""Переходный мост Telegram → VK: один текстовый пост, фото владелец добавляет вручную.

Источник читается строго read-only из БД уже работающего контент-завода. Отдельная БД
фиксирует отправленные ключи и не позволяет повторить публикацию при следующем запуске.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from content_factory.publish.vk import VK_MESSAGE_MAX, VkPublisher, adapt_vk_text


CLIMATE_TERMS = (
    "кондиц", "сплит", "тепловой насос", "тепловые насос", "рекуператор",
    "вентиляц", "стабилизатор",
)
BLOCKED_TERMS = (
    "⛔ продано", "продано", "нет в наличии",
    # Расходники полезны позже, но не должны вытеснять основное оборудование
    # при первичном наполнении нового сообщества.
    "труба медная", "кабель", "дренаж", "кронштейн", "виброопор", "фреон",
)


@dataclass(frozen=True)
class VkTextCandidate:
    key: str
    source_ts: float
    caption: str
    card_path: str


@dataclass(frozen=True)
class VkTextSyncResult:
    ok: bool
    skipped: bool = False
    source_key: str | None = None
    post_id: int | None = None
    manual_photo_path: str | None = None
    error: str | None = None


class VkTextSyncState:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as c:
            c.execute(
                "CREATE TABLE IF NOT EXISTS vk_text_sync ("
                "source_key TEXT PRIMARY KEY, post_id INTEGER NOT NULL, "
                "source_ts REAL NOT NULL, card_path TEXT NOT NULL, "
                "status TEXT NOT NULL DEFAULT 'manual_photo_pending', ts REAL NOT NULL)"
            )

    def contains(self, key: str) -> bool:
        with sqlite3.connect(self.path) as c:
            row = c.execute(
                "SELECT 1 FROM vk_text_sync WHERE source_key=?", (key,)
            ).fetchone()
        return row is not None

    def mark(self, candidate: VkTextCandidate, post_id: int) -> None:
        with sqlite3.connect(self.path) as c:
            c.execute(
                "INSERT INTO vk_text_sync"
                "(source_key, post_id, source_ts, card_path, status, ts) "
                "VALUES(?,?,?,?,?,?)",
                (candidate.key, int(post_id), candidate.source_ts, candidate.card_path,
                 "manual_photo_pending", time.time()),
            )

    def seed(self, source_key: str, post_id: int, card_path: str = "") -> None:
        candidate = VkTextCandidate(source_key, 0.0, "", card_path)
        if not self.contains(source_key):
            self.mark(candidate, post_id)


def _published_rows(source_db: str | Path):
    # URI mode=ro защищает рабочую БД Telegram-контент-завода от случайной записи.
    uri = f"file:{Path(source_db).resolve().as_posix()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as c:
        return c.execute(
            "SELECT p.key, p.ts, p.caption, COALESCE(a.card_path, '') "
            "FROM published p LEFT JOIN awaiting a ON a.key=p.key "
            "WHERE COALESCE(p.status, 'active')='active' "
            "ORDER BY p.ts DESC"
        ).fetchall()


def next_candidate(source_db: str | Path, state: VkTextSyncState) -> VkTextCandidate | None:
    """Выбрать свежий активный климатический пост с карточкой, исключив дубли/проданное."""
    for key, source_ts, caption, card_path in _published_rows(source_db):
        plain = adapt_vk_text(caption or "").lower()
        if not any(term in plain for term in CLIMATE_TERMS):
            continue
        if any(term in plain for term in BLOCKED_TERMS):
            continue
        if not card_path or not Path(card_path).is_file() or state.contains(str(key)):
            continue
        return VkTextCandidate(str(key), float(source_ts or 0), caption or "", card_path)
    return None


def build_vk_climate_text(caption: str) -> str:
    """Адаптировать готовый товарный материал под читаемый пост сообщества VK."""
    source = adapt_vk_text(caption)
    lines = []
    for raw in source.splitlines():
        line = raw.strip()
        if line and re.fullmatch(r"[═=_—-]{5,}", line):
            continue
        lines.append(raw.rstrip())
    body = "\n".join(lines).strip()
    body = re.sub(r"\n{3,}", "\n\n", body)
    footer = (
        "Подберём модель под площадь, режим эксплуатации и бюджет.\n"
        "📍 Подбор, монтаж и обслуживание по Крыму\n"
        "🚚 Доставка в Запорожскую и Херсонскую области\n"
        "📞 +7 978 579-29-95\n"
        "🌐 splithome.ru"
    )
    room = VK_MESSAGE_MAX - len(footer) - 2
    return f"{body[:room].rstrip()}\n\n{footer}".strip()


def sync_one(source_db: str | Path, state: VkTextSyncState,
             publisher: VkPublisher) -> VkTextSyncResult:
    candidate = next_candidate(source_db, state)
    if candidate is None:
        return VkTextSyncResult(ok=True, skipped=True)
    result = publisher.publish_text(build_vk_climate_text(candidate.caption))
    if not result.ok:
        return VkTextSyncResult(
            ok=False, source_key=candidate.key, manual_photo_path=candidate.card_path,
            error=result.error,
        )
    if not result.dry_run and result.post_id is not None:
        state.mark(candidate, result.post_id)
    return VkTextSyncResult(
        ok=True, skipped=result.dry_run, source_key=candidate.key,
        post_id=result.post_id, manual_photo_path=candidate.card_path,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Один безопасный текстовый пост VK за запуск")
    parser.add_argument("--source-db", default=os.getenv(
        "CF_SOURCE_DB", "/opt/content-factory/state/content_factory.db"))
    parser.add_argument("--state-db", default=os.getenv(
        "VK_SYNC_STATE_DB", "/opt/content-factory-vk/state/vk-sync.db"))
    parser.add_argument("--owner-id", type=int, default=int(os.getenv(
        "VK_OWNER_ID", "-241020718")))
    parser.add_argument("--publish", action="store_true",
                        help="без флага выполняется только безопасный dry-run")
    parser.add_argument("--seed", nargs=2, metavar=("SOURCE_KEY", "POST_ID"))
    args = parser.parse_args()

    state = VkTextSyncState(args.state_db)
    if args.seed:
        state.seed(args.seed[0], int(args.seed[1]))
        print(json.dumps({"seeded": args.seed[0]}, ensure_ascii=False))
        return

    publisher = VkPublisher(
        os.getenv("VK_ACCESS_TOKEN", ""), args.owner_id,
        dry_run=not args.publish,
    )
    result = sync_one(args.source_db, state, publisher)
    print(json.dumps(asdict(result), ensure_ascii=False))
    if not result.ok:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
