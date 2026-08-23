from urllib.parse import parse_qs

import httpx

from content_factory.publish.vk import VkPublisher, adapt_vk_text, build_vk_share_url
from content_factory.pilots.vk_preview import build_vk_preview
from content_factory.orchestrator.confirm_store import Awaiting
from content_factory.config import load_config


def test_adapt_vk_text_removes_telegram_html_and_preserves_content():
    text = adapt_vk_text("Товар\n<blockquote>💎 <b>22 900 ₽</b></blockquote>\nОписание")
    assert "<" not in text and ">" not in text
    assert "22 900 ₽" in text and "Описание" in text


def test_adapt_vk_text_separates_model_from_catalog_suffix():
    assert "SRW-12000-D однофазный" in adapt_vk_text("SRW-12000-Dоднофазный")


def test_vk_dry_run_makes_payload_without_network():
    def handler(request):
        raise AssertionError("dry-run не должен обращаться к VK")
    http = httpx.Client(transport=httpx.MockTransport(handler))
    result = VkPublisher("", 22223507, dry_run=True, http=http).publish("card.png", "Текст")
    assert result.ok and result.dry_run
    assert result.payload == {"owner_id": 22223507, "message": "Текст",
                              "image": "card.png", "dry_run": True}


def test_vk_share_url_contains_public_page_and_image():
    share = build_vk_share_url(
        url="https://climat-simf.ru/",
        title="RUCELF SRW-12000-D",
        description="Цена 22 900 ₽",
        image_url="https://splithome.ru/static/cf-cards/item.png",
    )
    query = parse_qs(share.split("?", 1)[1])
    assert share.startswith("https://vk.com/share.php?")
    assert query["url"] == ["https://climat-simf.ru/"]
    assert query["image"] == ["https://splithome.ru/static/cf-cards/item.png"]
    assert query["comment"] == ["Цена 22 900 ₽"]


def test_vk_publish_uploads_photo_then_posts_wall(tmp_path):
    card = tmp_path / "card.png"
    card.write_bytes(b"PNG")
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path.endswith("photos.getWallUploadServer"):
            return httpx.Response(200, json={"response": {"upload_url": "https://upload.vk.test/u"}})
        if request.url.host == "upload.vk.test":
            return httpx.Response(200, json={"server": 1, "photo": "[]", "hash": "h"})
        if request.url.path.endswith("photos.saveWallPhoto"):
            return httpx.Response(200, json={"response": [{"owner_id": 22223507, "id": 77}]})
        if request.url.path.endswith("wall.post"):
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            assert form["attachments"] == "photo22223507_77"
            return httpx.Response(200, json={"response": {"post_id": 88}})
        raise AssertionError(request.url)

    http = httpx.Client(transport=httpx.MockTransport(handler))
    result = VkPublisher("TOKEN", 22223507, dry_run=False, http=http).publish(card, "Пост")
    assert result.ok and result.post_id == 88
    assert calls == ["/method/photos.getWallUploadServer", "/u",
                     "/method/photos.saveWallPhoto", "/method/wall.post"]


def test_build_vk_preview_from_review_record(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "source: {}\nvk: {enabled: true, owner_id: 22223507, dry_run: true}\n",
        encoding="utf-8")
    cfg = load_config(config_path)
    awaiting = Awaiting("storefront:1", "@channel", "/cards/1.png",
                        "Товар\n<blockquote><b>100 ₽</b></blockquote>", "published")
    payload = build_vk_preview(cfg, awaiting)
    assert payload["owner_id"] == 22223507
    assert payload["message"] == "Товар\n\n100 ₽"
    assert payload["dry_run"] is True


def test_build_vk_preview_adds_manual_share_link(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "source: {}\n"
        "vk:\n"
        "  enabled: true\n"
        "  owner_id: 22223507\n"
        "  dry_run: true\n"
        "  share_url: https://climat-simf.ru/\n"
        "  public_image_base_url: https://splithome.ru/static/cf-cards\n",
        encoding="utf-8")
    awaiting = Awaiting("storefront:1", "@channel", "/cards/item.png",
                        "Товар\nЦена", "published")
    payload = build_vk_preview(load_config(config_path), awaiting)
    assert payload["public_image_url"].endswith("/item.png")
    assert payload["share_url"].startswith("https://vk.com/share.php?")
