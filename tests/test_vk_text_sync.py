import sqlite3
from pathlib import Path

from content_factory.publish.vk import VkPublishResult
from content_factory.publish.vk_text_sync import (
    VkTextSyncState,
    build_vk_climate_text,
    next_candidate,
    sync_one,
)


def _source_db(path: Path):
    card = path.parent / "xigma.jpg"
    card.write_bytes(b"JPEG")
    with sqlite3.connect(path) as c:
        c.execute(
            "CREATE TABLE published (key TEXT PRIMARY KEY, message_id INTEGER, ts REAL, "
            "channel TEXT, price INTEGER, status TEXT, caption TEXT)"
        )
        c.execute(
            "CREATE TABLE awaiting (key TEXT PRIMARY KEY, channel TEXT, card_path TEXT, "
            "caption TEXT, status TEXT, ts REAL)"
        )
        rows = [
            ("breeze|xigma|sky", 1, 30.0, "@tg", None, "active",
             "XIGMA Сплит-система SKY\n<blockquote><b>от 12 090 ₽</b></blockquote>"),
            ("manual|sold", 2, 40.0, "@tg", None, "sold",
             "⛔ ПРОДАНО\nКондиционер MBO"),
            ("manual|kettle", 3, 50.0, "@tg", None, "active", "Чайник 1,8 л"),
        ]
        c.executemany("INSERT INTO published VALUES(?,?,?,?,?,?,?)", rows)
        c.executemany(
            "INSERT INTO awaiting VALUES(?,?,?,?,?,?)",
            [
                ("breeze|xigma|sky", "@tg", str(card), "", "published", 30.0),
                ("manual|sold", "@tg", "/cards/sold.jpg", "", "published", 40.0),
                ("manual|kettle", "@tg", "/cards/kettle.jpg", "", "published", 50.0),
            ],
        )


def test_next_candidate_uses_active_climate_content_and_skips_synced(tmp_path):
    source = tmp_path / "source.db"
    _source_db(source)
    state = VkTextSyncState(tmp_path / "vk.db")

    candidate = next_candidate(source, state)
    assert candidate.key == "breeze|xigma|sky"
    assert candidate.card_path == str(tmp_path / "xigma.jpg")

    state.mark(candidate, post_id=7)
    assert next_candidate(source, state) is None


def test_build_vk_climate_text_is_clean_and_has_professional_cta():
    text = build_vk_climate_text(
        "FUNAI Сплит-система\n<blockquote>💎 <b>24 190 ₽</b></blockquote>\n"
        "════════════\nКлючевые особенности:\n✓ До 20 м²"
    )
    assert "<blockquote>" not in text
    assert "════" not in text
    assert "✓ До 20 м²" in text
    assert "Подберём модель под площадь" in text
    assert "splithome.ru" in text
    assert "+7 978 579-29-95" in text


def test_sync_one_publishes_one_text_post_and_records_manual_photo(tmp_path):
    source = tmp_path / "source.db"
    _source_db(source)
    state = VkTextSyncState(tmp_path / "vk.db")

    class Publisher:
        def __init__(self):
            self.messages = []

        def publish_text(self, message):
            self.messages.append(message)
            return VkPublishResult(ok=True, post_id=11)

    publisher = Publisher()
    result = sync_one(source, state, publisher)
    assert result.ok and result.post_id == 11
    assert result.source_key == "breeze|xigma|sky"
    assert result.manual_photo_path == str(tmp_path / "xigma.jpg")
    assert len(publisher.messages) == 1
    assert next_candidate(source, state) is None
