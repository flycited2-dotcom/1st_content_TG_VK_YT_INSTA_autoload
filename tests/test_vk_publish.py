from urllib.parse import parse_qs

import httpx

from content_factory.publish.vk import (
    VkPublisher,
    adapt_vk_text,
    build_stabilizer_vk_comment,
    build_vk_share_url,
)
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


def test_vk_share_url_uses_open_graph_page_and_comment_only():
    share = build_vk_share_url(
        url="https://climat-simf.ru/",
        title="RUCELF SRW-12000-D",
        description="Цена 22 900 ₽",
        image_url="https://splithome.ru/static/cf-cards/item.png",
    )
    query = parse_qs(share.split("?", 1)[1])
    assert share.startswith("https://vk.com/share.php?")
    assert query["url"] == ["https://climat-simf.ru/"]
    assert "image" not in query
    assert "title" not in query
    assert "description" not in query
    assert query["comment"] == ["Цена 22 900 ₽"]


def test_stabilizer_vk_comment_expands_usp_and_contacts():
    text = build_stabilizer_vk_comment(
        "RUCELF SRW-12000-D\n💎 22 900 ₽\nМощность: 12000 ВА")
    assert "Рабочий диапазон 130–270 В" in text
    assert "Гарантия производителя 36 месяцев" in text
    assert "Крыму, Запорожской и Херсонской областям" in text
    assert "+7 978 579-29-95" in text
    assert "Почему стоит выбрать:\r\n✅ Защищает технику" in text
    assert len([line for line in text.split("\r\n") if line.startswith("✅ ")]) == 7


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


def test_vk_group_wall_uses_group_target_and_stops_if_token_cannot_upload(tmp_path):
    card = tmp_path / "card.png"
    card.write_bytes(b"PNG")
    def handler(request):
        assert request.url.path.endswith("photos.getWallUploadServer")
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        assert form["group_id"] == "241020718"
        return httpx.Response(200, json={"error": {
            "error_code": 27, "error_msg": "Group authorization failed",
        }})

    http = httpx.Client(transport=httpx.MockTransport(handler))
    result = VkPublisher("GROUP_TOKEN", -241020718, dry_run=False, http=http).publish(
        card, "Пост сообщества")
    assert not result.ok
    assert result.post_id is None
    assert "VK 27" in result.error


def test_vk_user_token_uploads_to_group_and_schedules_from_group(tmp_path):
    card = tmp_path / "card.png"
    card.write_bytes(b"PNG")

    def handler(request):
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        if request.url.path.endswith("photos.getWallUploadServer"):
            assert form["group_id"] == "241020718"
            return httpx.Response(200, json={"response": {"upload_url": "https://upload.vk.test/u"}})
        if request.url.host == "upload.vk.test":
            return httpx.Response(200, json={"server": 1, "photo": "[]", "hash": "h"})
        if request.url.path.endswith("photos.saveWallPhoto"):
            assert form["group_id"] == "241020718"
            return httpx.Response(200, json={"response": [{"owner_id": -241020718, "id": 77}]})
        if request.url.path.endswith("wall.post"):
            assert form["owner_id"] == "-241020718"
            assert form["from_group"] == "1"
            assert form["publish_date"] == "1787657400"
            return httpx.Response(200, json={"response": {"post_id": 88}})
        raise AssertionError(request.url)

    http = httpx.Client(transport=httpx.MockTransport(handler))
    result = VkPublisher("USER_TOKEN", -241020718, dry_run=False, http=http).publish(
        card, "Пост", publish_at=1787657400)
    assert result.ok and result.post_id == 88


def test_vk_group_can_explicitly_publish_text_for_manual_photo_flow():
    def handler(request):
        assert request.url.path.endswith("wall.post")
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        assert form == {
            "owner_id": "-241020718",
            "from_group": "1",
            "message": "Товар\n\nЦена 20 000 ₽",
            "access_token": "GROUP_TOKEN",
            "v": "5.199",
        }
        return httpx.Response(200, json={"response": {"post_id": 9}})

    http = httpx.Client(transport=httpx.MockTransport(handler))
    result = VkPublisher("GROUP_TOKEN", -241020718, dry_run=False, http=http).publish_text(
        "Товар\n<blockquote>Цена <b>20 000 ₽</b></blockquote>")
    assert result.ok and result.post_id == 9
    assert result.payload["manual_photo_required"] is True


def test_vk_group_can_schedule_text_for_manual_photo_flow():
    def handler(request):
        assert request.url.path.endswith("wall.post")
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        assert form["publish_date"] == "1787657400"
        return httpx.Response(200, json={"response": {"post_id": 10}})

    http = httpx.Client(transport=httpx.MockTransport(handler))
    result = VkPublisher("GROUP_TOKEN", -241020718, dry_run=False, http=http).publish_text(
        "Отложенный пост", publish_at=1787657400)

    assert result.ok and result.post_id == 10
    assert result.payload["publish_date"] == 1787657400


def test_vk_edits_existing_post_without_replacing_attachments():
    def handler(request):
        assert request.url.path.endswith("wall.edit")
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        assert form["owner_id"] == "-241020718"
        assert form["post_id"] == "19"
        assert form["publish_date"] == "1787931000"
        assert form["message"] == "Обновлённый пост"
        assert "attachments" not in form
        return httpx.Response(200, json={"response": 1})

    http = httpx.Client(transport=httpx.MockTransport(handler))
    result = VkPublisher("TOKEN", -241020718, dry_run=False, http=http).edit_text(
        19, "Обновлённый пост", publish_at=1787931000,
    )
    assert result.ok and result.post_id == 19


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
        "  share_url: https://climat-simf.ru/share/vk\n"
        "  public_image_base_url: https://splithome.ru/static/cf-cards\n",
        encoding="utf-8")
    awaiting = Awaiting("storefront:1", "@channel", "/cards/item.png",
                        "Товар\nЦена", "published")
    payload = build_vk_preview(load_config(config_path), awaiting)
    assert payload["public_image_url"].endswith("/item.png")
    assert payload["share_page_url"] == "https://climat-simf.ru/share/vk/item"
    assert "Почему стоит выбрать" in payload["message"]
    assert payload["share_url"].startswith("https://vk.com/share.php?")
