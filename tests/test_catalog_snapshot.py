"""Автообновление снимка каталога из базы сайта: безопасное при любой неудаче."""
import json
import os
import subprocess
import time
from types import SimpleNamespace

import pytest

from content_factory.storefront.catalog_snapshot import (
    DUMP_SCRIPT,
    MARK_END,
    MARK_START,
    load_snapshot,
    parse_dump,
    refresh_snapshot,
    snapshot_age_hours,
)


def _row(n, **over):
    base = {"offer_id": f"breeze:НС-{1000 + n}", "slug": f"xigma-model-{n}-НС-{1000 + n}",
            "title": f"XIGMA Сплит-система серии SKY XG-SKY{n}", "price": 15000 + n,
            "category": "Бытовые сплит-системы", "brand": "XIGMA",
            "description": "Ночной режим работает при уровне шума от 24 дБ(А) и не мешает спать.",
            "picture": f"https://img.example/{n}.png", "specs": {"Инверторная технология": "Да"},
            "is_heat_pump": False, "heating_min_temp": None, "quantity": 3, "warehouse": "Симферополь"}
    base.update(over)
    return base


def _dump_output(rows):
    banner = "29 objects imported automatically (use -v 2 for details).\n"
    return banner + MARK_START + json.dumps(rows, ensure_ascii=False) + MARK_END + "\n"


def _runner(stdout=None, error=None):
    def run(cmd, **kwargs):
        assert cmd[:3] == ["docker", "exec", "-i"]
        assert "input" in kwargs and "python" in cmd
        if error:
            raise error
        return SimpleNamespace(stdout=stdout)
    return run


def test_dump_script_ships_with_the_package_and_only_reads():
    text = DUMP_SCRIPT.read_text(encoding="utf-8")

    assert MARK_START in text and MARK_END in text
    assert not any(word in text for word in (".save(", ".delete(", ".update(", ".create(", "bulk_"))


def test_parse_dump_ignores_the_shell_banner_and_rejects_garbage():
    assert parse_dump(_dump_output([_row(1)]))[0]["price"] == 15001
    with pytest.raises(ValueError):
        parse_dump("Traceback (most recent call last): ...")


def test_stale_snapshot_is_refreshed_atomically(tmp_path):
    path = tmp_path / "snap.json"
    path.write_text("[]", encoding="utf-8")
    old = time.time() - 30 * 3600
    os.utime(path, (old, old))
    rows = [_row(n) for n in range(600)]

    result = refresh_snapshot(path, run=_runner(_dump_output(rows)))

    assert result == {"status": "refreshed", "items": 600}
    assert len(json.loads(path.read_text(encoding="utf-8"))) == 600
    assert not (tmp_path / "snap.json.tmp").exists()


def test_fresh_snapshot_is_left_alone(tmp_path):
    path = tmp_path / "snap.json"
    path.write_text("[]", encoding="utf-8")

    def boom(*args, **kwargs):
        raise AssertionError("свежий снимок не должен перезапрашиваться")

    assert refresh_snapshot(path, run=boom)["status"] == "fresh"


@pytest.mark.parametrize("error", [
    subprocess.TimeoutExpired("docker", 1), FileNotFoundError("docker"),
    subprocess.CalledProcessError(1, "docker"),
])
def test_failed_refresh_keeps_the_previous_snapshot(tmp_path, error):
    path = tmp_path / "snap.json"
    path.write_text('["старый"]', encoding="utf-8")
    old = time.time() - 40 * 3600
    os.utime(path, (old, old))

    result = refresh_snapshot(path, run=_runner(error=error))

    assert result["status"].startswith("failed") and result["kept_old"] is True
    assert path.read_text(encoding="utf-8") == '["старый"]'


def test_a_suspiciously_small_dump_is_rejected(tmp_path):
    path = tmp_path / "snap.json"

    result = refresh_snapshot(path, run=_runner(_dump_output([_row(1), _row(2)])))

    assert result["status"].startswith("rejected") and not path.exists()


def test_missing_snapshot_has_no_age(tmp_path):
    assert snapshot_age_hours(tmp_path / "nope.json") is None


def test_snapshot_rows_become_retail_catalog_items(tmp_path):
    path = tmp_path / "snap.json"
    rows = [
        _row(1),
        _row(2, title="Кронштейн для наружного блока", price=4000),
        _row(3, category="Мульти сплит-системы"),
        _row(4, is_heat_pump=True, heating_min_temp=-15),
        _row(5, picture=""),
    ]
    path.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")

    items = load_snapshot(path)

    assert [item.id for item in items] == ["breeze:NS-1001", "breeze:NS-1004"]
    first, heat = items
    assert first.url == "https://splithome.ru/product/xigma-model-1-%D0%9D%D0%A1-1001/"
    assert first.group == "ac" and first.brand == "xigma"
    assert heat.attrs["Тепловой насос"] == "Да"
    assert heat.attrs["Минимальная температура обогрева"] == "−15 °C"
