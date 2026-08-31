import hashlib
import json

import httpx
import pytest

from content_factory.bot.run import resolve_callback_data
from content_factory.ingest import avito_import as imp
from content_factory.orchestrator.confirm_store import ConfirmStore
from content_factory.publish.orders import OrderLinks
from content_factory.publish.telegram import PublishState

PNG = b"\x89PNG\r\n\x1a\n" + b"generated-card" * 4
USP = "9 секций, мощность 2 кВт.\n\nНапишите в Avito для заказа."


def _sha(blob: bytes) -> str:
    return hashlib.sha256(blob).hexdigest()


def _raw(root, source_id="AV-1", *, category="oil_radiator", price=9990, blob=PNG):
    rel = f"cards/{source_id}.png"
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(blob)
    return {"source_id": source_id, "sku": f"NC-{source_id}", "series_key": "",
            "category": category, "availability": "in_stock",
            "title": f"Радиатор {source_id}", "brand": "Ballu",
            "card": {"path": rel, "sha256": _sha(blob), "provenance": "generated",
                     "generator": "fotogen", "job_id": f"job-{source_id}"},
            "usp": {"kind": "generator_override", "text": USP,
                    "source_ref": f"manifest.json#{source_id}",
                    "sha256": _sha(USP.encode())},
            "price": {"final": price, "currency": "RUB", "kind": "exact",
                      "already_marked_up": True}}


def _manifest(tmp_path, items):
    root = tmp_path / "src-cards"
    root.mkdir(exist_ok=True)
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(
        {"schema_version": 1, "batch_id": "b-2026-08-30", "cards_root": str(root),
         "feed": {"url": "https://splithome.ru/static/avito-feed.xml",
                  "sha256": _sha(b"feed")},
         "items": [item(root) if callable(item) else item for item in items]},
        ensure_ascii=False), encoding="utf-8")
    return path


def _ok(message_id=101):
    return httpx.Response(200, json={"ok": True, "result": {"message_id": message_id}})


class _Telegram:
    """Клиент на MockTransport: очередь ответов/исключений + журнал запросов."""

    def __init__(self, responses=()):
        self.queue = list(responses)
        self.requests = []
        self.client = httpx.Client(transport=httpx.MockTransport(self._handle))

    def _handle(self, request):
        self.requests.append(request)
        item = self.queue.pop(0) if self.queue else _ok(100 + len(self.requests))
        if isinstance(item, Exception):
            raise item
        return item


def _photos(tg):
    """Только отправки постов в review: отчёт владельцу идёт другим методом."""
    return [r for r in tg.requests if "sendPhoto" in str(r.url)]


def _to_owner(tg, owner="1264067528"):
    """Запросы, ушедшие владельцу в личку."""
    return [r for r in tg.requests
            if "sendPhoto" not in str(r.url)
            and owner in r.content.decode("utf-8", "replace")]


class _Exploding:
    def post(self, *a, **kw):                      # pragma: no cover - страховка теста
        raise AssertionError("сеть не должна использоваться")


def _run(tmp_path, items, *, tg=None, dry_run=False, sleeps=None, **over):
    kwargs = dict(state_db=tmp_path / "state" / "cf.db", media_dir=tmp_path / "media",
                  telegram_token="TOK", review_chat="-100111",
                  publish_channel="-100999", owner_chat="1264067528",
                  dry_run=dry_run, limit=12, min_interval=0,
                  http=(tg.client if isinstance(tg, _Telegram) else tg),
                  sleep=(sleeps.append if sleeps is not None else (lambda _s: None)))
    kwargs.update(over)
    return imp.run_import(_manifest(tmp_path, items), **kwargs)


def _db(tmp_path):
    return tmp_path / "state" / "cf.db"


# ── dry-run и предполётные проверки ─────────────────────────────────────────────
def test_dry_run_writes_nothing_and_sends_nothing(tmp_path):
    summary = _run(tmp_path, [_raw], tg=_Exploding(), dry_run=True)
    assert summary["mode"] == "dry-run" and summary["counts"]["prepared"] == 1
    assert not (tmp_path / "state").exists()
    assert not (tmp_path / "media").exists()


def test_review_channel_equal_to_publish_channel_aborts_before_network(tmp_path):
    with pytest.raises(imp.PreflightError, match="совпадает"):
        _run(tmp_path, [_raw], tg=_Exploding(), publish_channel="-100111")
    assert not _db(tmp_path).exists()


def test_channels_in_different_forms_are_not_assumed_distinct(tmp_path):
    with pytest.raises(imp.PreflightError, match="не доказуемо"):
        _run(tmp_path, [_raw], tg=_Exploding(), publish_channel="@climat_channel")


def test_send_is_refused_when_owner_gate_would_be_permissive(tmp_path):
    with pytest.raises(imp.PreflightError, match="owner-гейт"):
        _run(tmp_path, [_raw], tg=_Exploding(), owner_chat="")


def test_channels_distinct_rules():
    assert imp.channels_distinct("-100111", "-100999")[0] is True
    assert imp.channels_distinct("@a", "@b")[0] is True
    assert imp.channels_distinct("@Chan", "https://t.me/chan")[0] is False
    assert imp.channels_distinct("", "-100999")[0] is False


# ── успешная отправка в review ──────────────────────────────────────────────────
def test_review_send_creates_pending_row_with_stable_copy_and_real_message_id(tmp_path):
    tg = _Telegram([_ok(555)])
    summary = _run(tmp_path, [_raw], tg=tg)

    assert summary["counts"]["sent"] == 1
    assert tg.requests[0].url.path == "/botTOK/sendPhoto"
    row = ConfirmStore(_db(tmp_path)).get("avito|AV-1")
    assert row.status == "pending"
    assert row.channel == "-100999"               # approve опубликует в боевой канал
    card = tmp_path / "media" / "b-2026-08-30" / "avito_AV-1.png"
    assert row.card_path == str(card.resolve()) and card.is_file()
    assert (tmp_path / "src-cards" / "cards" / "AV-1.png").is_file()   # оригинал цел
    assert imp.REVIEW_NOTE not in row.caption      # в канал уйдёт чистая подпись
    delivery = imp.DeliveryJournal(_db(tmp_path)).get(summary["sent"][0]["import_key"])
    assert (delivery.status, delivery.review_message_id) == ("sent", 555)


def test_importer_never_marks_anything_published(tmp_path):
    _run(tmp_path, [_raw], tg=_Telegram())
    assert PublishState(_db(tmp_path)).published_keys() == set()
    assert ConfirmStore(_db(tmp_path)).get("avito|AV-1").status == "pending"


def test_review_button_code_resolves_back_to_the_confirm_key(tmp_path):
    _run(tmp_path, [_raw], tg=_Telegram())
    store, links = ConfirmStore(_db(tmp_path)), OrderLinks(_db(tmp_path))
    code = links.code_for("avito|AV-1")
    assert resolve_callback_data(f"approve:{code}", store, links) == "approve:avito|AV-1"


def test_review_required_categories_are_reported_separately(tmp_path):
    summary = _run(tmp_path, [lambda r: _raw(r, "AV-9", category="convector_electric")],
                   tg=_Telegram())
    assert summary["review_required"] == ["avito|AV-9"]


# ── дедупликация и защита состояния ─────────────────────────────────────────────
def test_second_run_does_not_send_again(tmp_path):
    tg = _Telegram()
    _run(tmp_path, [_raw], tg=tg)
    second = _run(tmp_path, [_raw], tg=tg)
    assert len(_photos(tg)) == 1
    assert second["counts"]["sent"] == 0
    assert second["skipped"][0]["reason"] in {"confirm_state:pending", "delivery:sent"}


def test_rejected_row_is_never_overwritten(tmp_path):
    store = ConfirmStore(_db(tmp_path))
    store.add("avito|AV-1", "-100999", "/old/card.png", "старая подпись")
    store.mark("avito|AV-1", "rejected")
    tg = _Telegram()
    summary = _run(tmp_path, [_raw], tg=tg)
    row = store.get("avito|AV-1")
    assert (row.status, row.card_path, row.caption) == ("rejected", "/old/card.png",
                                                        "старая подпись")
    assert _photos(tg) == [] and summary["skipped"][0]["reason"] == "confirm_state:rejected"


def test_published_product_is_skipped(tmp_path):
    PublishState(_db(tmp_path)).mark("avito|AV-1", 7, channel="-100999")
    tg = _Telegram()
    summary = _run(tmp_path, [_raw], tg=tg)
    assert _photos(tg) == [] and summary["skipped"][0]["reason"] == "already_published"


def test_batch_limit_and_deterministic_order(tmp_path):
    items = [lambda r: _raw(r, "AV-3", category="thermostat", price=3000),
             lambda r: _raw(r, "AV-1", category="heat_pump", price=90000),
             lambda r: _raw(r, "AV-2", category="oil_radiator", price=5000)]
    tg = _Telegram()
    summary = _run(tmp_path, items, tg=tg, limit=2)
    assert [s["key"] for s in summary["sent"]] == ["avito|AV-1", "avito|AV-2"]
    assert summary["skipped"][-1] == {"key": "avito|AV-3", "reason": "batch_limit",
                                      "detail": "лимит партии 2"}


# ── ошибки доставки ─────────────────────────────────────────────────────────────
def test_definite_telegram_error_leaves_item_retryable(tmp_path):
    tg = _Telegram([httpx.Response(200, json={"ok": False, "description": "chat not found"})])
    summary = _run(tmp_path, [_raw], tg=tg)
    assert summary["counts"]["failed"] == 1 and summary["counts"]["sent"] == 0
    assert ConfirmStore(_db(tmp_path)).get("avito|AV-1").status == "send_failed"
    assert PublishState(_db(tmp_path)).published_keys() == set()

    retry = _run(tmp_path, [_raw], tg=_Telegram([_ok(77)]))
    assert retry["counts"]["sent"] == 1


def test_rate_limit_waits_retry_after_then_retries_once(tmp_path):
    sleeps = []
    tg = _Telegram([httpx.Response(429, json={"ok": False, "parameters": {"retry_after": 2}}),
                    _ok(42)])
    summary = _run(tmp_path, [_raw], tg=tg, sleeps=sleeps)
    assert sleeps == [2.0] and len(_photos(tg)) == 2
    assert summary["sent"][0]["message_id"] == 42


def test_rate_limit_without_retry_after_is_not_retried(tmp_path):
    tg = _Telegram([httpx.Response(429, json={"ok": False})])
    summary = _run(tmp_path, [_raw], tg=tg)
    assert len(_photos(tg)) == 1 and summary["counts"]["failed"] == 1
    assert ConfirmStore(_db(tmp_path)).get("avito|AV-1").status == "send_failed"


def test_connect_error_is_a_definite_failure(tmp_path):
    tg = _Telegram([httpx.ConnectError("нет соединения")])
    summary = _run(tmp_path, [_raw], tg=tg)
    assert summary["counts"]["failed"] == 1 and summary["counts"]["unverified"] == 0
    assert ConfirmStore(_db(tmp_path)).get("avito|AV-1").status == "send_failed"


def test_ambiguous_timeout_stops_the_batch_and_keeps_pending(tmp_path):
    tg = _Telegram([httpx.ReadTimeout("таймаут чтения")])
    summary = _run(tmp_path, [_raw, lambda r: _raw(r, "AV-2")], tg=tg)
    assert summary["counts"]["unverified"] == 1 and summary["stopped"]
    assert len(_photos(tg)) == 1                     # вторая позиция не отправлялась
    store = ConfirmStore(_db(tmp_path))
    assert store.get("avito|AV-1").status == "pending"   # кнопка обязана работать
    assert store.get("avito|AV-2") is None
    journal = imp.DeliveryJournal(_db(tmp_path))
    assert journal.get(summary["unverified"][0]["import_key"]).status == "send_unverified"


def test_server_error_is_ambiguous_not_definite(tmp_path):
    tg = _Telegram([httpx.Response(503, text="bad gateway")])
    summary = _run(tmp_path, [_raw], tg=tg)
    assert summary["counts"]["unverified"] == 1 and summary["counts"]["failed"] == 0


def test_success_without_message_id_is_treated_as_ambiguous(tmp_path):
    tg = _Telegram([httpx.Response(200, json={"ok": True, "result": {}})])
    summary = _run(tmp_path, [_raw], tg=tg)
    assert summary["counts"]["unverified"] == 1
    assert ConfirmStore(_db(tmp_path)).get("avito|AV-1").status == "pending"


def test_unverified_delivery_is_never_resent_automatically(tmp_path):
    tg = _Telegram([httpx.ReadTimeout("таймаут")])
    _run(tmp_path, [_raw], tg=tg)
    again = _run(tmp_path, [_raw], tg=tg)
    assert len(_photos(tg)) == 1
    assert again["skipped"][0]["reason"] == "confirm_state:pending"


# ── ручная сверка неоднозначных доставок ────────────────────────────────────────
def test_reconcile_lists_unverified_deliveries(tmp_path):
    _run(tmp_path, [_raw], tg=_Telegram([httpx.ReadTimeout("таймаут")]))
    report = imp.reconcile(_db(tmp_path))
    assert report["unverified"][0]["product_key"] == "avito|AV-1"


def test_operator_confirms_delivery_with_a_real_message_id(tmp_path):
    summary = _run(tmp_path, [_raw], tg=_Telegram([httpx.ReadTimeout("таймаут")]))
    key = summary["unverified"][0]["import_key"]
    assert imp.resolve_unverified(_db(tmp_path), key, "delivered")["ok"] is False
    result = imp.resolve_unverified(_db(tmp_path), key, "delivered", 321)
    assert result["ok"] and imp.DeliveryJournal(_db(tmp_path)).get(key).status == "sent"
    assert ConfirmStore(_db(tmp_path)).get("avito|AV-1").status == "pending"


def test_operator_reports_not_delivered_and_item_becomes_retryable(tmp_path):
    summary = _run(tmp_path, [_raw], tg=_Telegram([httpx.ReadTimeout("таймаут")]))
    key = summary["unverified"][0]["import_key"]
    assert imp.resolve_unverified(_db(tmp_path), key, "not-delivered")["ok"]
    assert ConfirmStore(_db(tmp_path)).get("avito|AV-1").status == "send_failed"
    retry = _run(tmp_path, [_raw], tg=_Telegram([_ok(999)]))
    assert retry["sent"][0]["message_id"] == 999


# ── дедупликация по «родным» ключам товара ──────────────────────────────────────
def _raw_with_series(root, source_id="AV-9", series_key="breeze|ballu|classic"):
    raw = _raw(root, source_id)
    raw["series_key"] = series_key
    return raw


def test_item_already_queued_under_its_series_key_is_not_sent_again(tmp_path):
    """Старый конвейер клал товар в очередь под series_key. Новый ключ avito|… не должен
    обходить эту защиту, иначе на один товар уйдёт второй пост."""
    ConfirmStore(_db(tmp_path)).add("breeze|ballu|classic", "-100999", "card.png", "текст")
    tg = _Telegram()
    summary = _run(tmp_path, [_raw_with_series], tg=tg)
    assert _photos(tg) == []
    assert summary["skipped"][0]["reason"] == "legacy_confirm_state:pending"
    assert summary["skipped"][0]["detail"] == "breeze|ballu|classic"


def test_item_already_published_under_its_series_key_is_not_sent_again(tmp_path):
    PublishState(_db(tmp_path)).mark("breeze|ballu|classic", 777, channel="-100999")
    tg = _Telegram()
    summary = _run(tmp_path, [_raw_with_series], tg=tg)
    assert _photos(tg) == []
    assert summary["skipped"][0]["reason"] == "legacy_published"


def test_item_queued_under_its_supplier_sku_is_not_sent_again(tmp_path):
    ConfirmStore(_db(tmp_path)).add("NC-AV-9", "-100999", "card.png", "текст")
    tg = _Telegram()
    summary = _run(tmp_path, [_raw_with_series], tg=tg)
    assert _photos(tg) == []
    assert summary["skipped"][0]["reason"] == "legacy_confirm_state:pending"


def test_free_series_key_does_not_block_a_new_item(tmp_path):
    """Чужая запись в очереди не должна мешать импорту."""
    ConfirmStore(_db(tmp_path)).add("breeze|other|series", "-100999", "card.png", "текст")
    tg = _Telegram()
    summary = _run(tmp_path, [_raw_with_series], tg=tg)
    assert len(summary["sent"]) == 1 and len(_photos(tg)) == 1


# ── отчёт о прогоне владельцу в личку ───────────────────────────────────────────
def test_owner_gets_the_report_after_the_batch(tmp_path):
    tg = _Telegram()
    summary = _run(tmp_path, [_raw], tg=tg)
    assert summary["report_sent"] is True
    calls = _to_owner(tg)
    assert calls, "владельцу не ушёл отчёт"
    assert "sendMessage" in str(calls[0].url)


def test_report_text_names_batch_counts_and_skip_reasons():
    summary = {"batch_id": "b-1", "mode": "send-review",
               "counts": {"manifest_items": 5, "selected": 4, "prepared": 0, "sent": 3,
                          "skipped": 1, "failed": 0, "unverified": 0, "review_required": 0},
               "skipped": [{"key": "avito|B", "reason": "no_generated_card", "detail": ""}],
               "failed": [], "unverified": [], "review_required": [], "stopped": None}
    text = imp.format_owner_report(summary)
    assert "b-1" in text and "no_generated_card" in text
    assert "3" in text and len(text) <= 4096


def test_report_names_items_that_never_made_it_into_the_batch():
    """Позиции без готовой карточки отсеиваются на сборке манифеста. В отчёте владельца
    они обязаны быть видны, иначе «160 пропавших» превращаются в молчание."""
    summary = {"batch_id": "b-1", "mode": "send-review",
               "counts": {"manifest_items": 44, "selected": 44, "prepared": 0, "sent": 44,
                          "skipped": 0, "failed": 0, "unverified": 0, "review_required": 0},
               "skipped": [], "failed": [], "unverified": [], "review_required": [],
               "stopped": None,
               "source_excluded": {"count": 160, "reasons": {"no_generated_card": 160}}}
    text = imp.format_owner_report(summary)
    assert "160" in text and "no_generated_card" in text


def test_report_flags_items_needing_a_decision():
    summary = {"batch_id": "b-1", "mode": "send-review",
               "counts": {"manifest_items": 1, "selected": 1, "prepared": 0, "sent": 0,
                          "skipped": 0, "failed": 1, "unverified": 1, "review_required": 1},
               "skipped": [], "failed": [{"key": "avito|A", "reason": "card_copy"}],
               "unverified": [{"key": "avito|B", "import_key": "k1"}],
               "review_required": ["avito|C"], "stopped": "останов"}
    text = imp.format_owner_report(summary)
    assert "avito|B" in text and "останов" in text


def test_no_report_in_dry_run(tmp_path):
    summary = _run(tmp_path, [_raw], tg=_Exploding(), dry_run=True)
    assert "report_sent" not in summary


def test_failed_report_does_not_break_a_successful_batch(tmp_path):
    """Отчёт вспомогателен: его сбой не отменяет уже отправленную партию."""
    tg = _Telegram([_ok(101), httpx.ConnectError("нет сети"),
                    httpx.ConnectError("нет сети")])
    summary = _run(tmp_path, [_raw], tg=tg)
    assert len(summary["sent"]) == 1
    assert summary["report_sent"] is False and summary["report_error"]


def test_dry_run_shows_the_caption_for_owner_review(tmp_path):
    """Владелец должен видеть фактический текст поста до отправки, а не только счётчики."""
    summary = _run(tmp_path, [_raw], tg=_Exploding(), dry_run=True)
    caption = summary["prepared"][0]["caption"]
    assert caption.splitlines()[0] == "Ballu Радиатор AV-1"
    assert "9 990 ₽" in caption
    assert "Напишите в Avito" not in caption          # avito-призыв вычищен
