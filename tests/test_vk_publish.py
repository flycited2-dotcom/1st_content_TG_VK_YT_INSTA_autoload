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


def _flaky_upload_transport(save_errors, calls, upload_statuses=()):
    """VK при сбое загрузки отдаёт photo=[] и saveWallPhoto отвечает 100: photo is undefined.
    save_errors — коды ошибок для первых вызовов saveWallPhoto, дальше успех;
    upload_statuses — HTTP-статусы первых ответов сервера загрузки (504 и т.п.)."""
    saves = iter(save_errors)
    uploads = iter(upload_statuses)

    def handler(request):
        name = request.url.path.rsplit("/", 1)[-1] if request.url.host != "upload.vk.test" else "upload"
        calls.append(name)
        if name == "photos.getWallUploadServer":
            return httpx.Response(200, json={"response": {"upload_url": "https://upload.vk.test/u"}})
        if name == "upload":
            status = next(uploads, 200)
            if status != 200:
                return httpx.Response(status, text="gateway")
            return httpx.Response(200, json={"server": 1, "photo": "[]", "hash": "h"})
        if name == "photos.saveWallPhoto":
            code = next(saves, None)
            if code:
                text = {100: "One of the parameters specified was missing or invalid: photo is undefined",
                        15: "Access denied"}[code]
                return httpx.Response(200, json={"error": {"error_code": code, "error_msg": text}})
            return httpx.Response(200, json={"response": [{"owner_id": -241020718, "id": 77}]})
        if name == "wall.post":
            return httpx.Response(200, json={"response": {"post_id": 88}})
        raise AssertionError(request.url)
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_vk_publish_retries_a_transient_empty_upload(tmp_path):
    """Разовый сбой «photo is undefined» не должен ронять публикацию: берётся новый адрес
    загрузки и фото грузится заново (на боевой стене так падало примерно каждое третье)."""
    card = tmp_path / "card.png"
    card.write_bytes(b"PNG")
    calls = []
    http = _flaky_upload_transport([100], calls)
    result = VkPublisher("T", -241020718, dry_run=False, http=http,
                         upload_retry_delay=0).publish(card, "Пост")
    assert result.ok and result.post_id == 88
    assert calls.count("photos.getWallUploadServer") == 2
    assert calls.count("wall.post") == 1


def test_vk_publish_gives_up_after_all_empty_uploads_without_posting(tmp_path):
    card = tmp_path / "card.png"
    card.write_bytes(b"PNG")
    calls = []
    attempts = VkPublisher.UPLOAD_ATTEMPTS
    http = _flaky_upload_transport([100] * attempts, calls)
    result = VkPublisher("T", -241020718, dry_run=False, http=http,
                         upload_retry_delay=0).publish(card, "Пост")
    assert not result.ok and "photo is undefined" in result.error
    assert calls.count("photos.getWallUploadServer") == attempts
    assert "wall.post" not in calls


def test_vk_publish_survives_several_failed_uploads_in_a_row(tmp_path):
    """В боевой день VK подряд отвечал 504 и photo=[] — трёх попыток не хватало."""
    card = tmp_path / "card.png"
    card.write_bytes(b"PNG")
    calls = []
    http = _flaky_upload_transport([100, 100], calls, upload_statuses=[504, 502])
    result = VkPublisher("T", -241020718, dry_run=False, http=http,
                         upload_retry_delay=0).publish(card, "Пост")
    assert result.ok and result.post_id == 88
    assert calls.count("photos.getWallUploadServer") == 5


def test_vk_publish_retries_a_gateway_timeout_of_the_upload_server(tmp_path):
    """Сервер загрузки VK однажды ответил 504: это тоже разовый сбой, повторяем с новым адресом."""
    card = tmp_path / "card.png"
    card.write_bytes(b"PNG")
    calls = []
    http = _flaky_upload_transport([], calls, upload_statuses=[504])
    result = VkPublisher("T", -241020718, dry_run=False, http=http,
                         upload_retry_delay=0).publish(card, "Пост")
    assert result.ok and result.post_id == 88
    assert calls.count("photos.getWallUploadServer") == 2


def test_vk_publish_does_not_retry_a_client_error_of_the_upload_server(tmp_path):
    card = tmp_path / "card.png"
    card.write_bytes(b"PNG")
    calls = []
    http = _flaky_upload_transport([], calls, upload_statuses=[413])
    result = VkPublisher("T", -241020718, dry_run=False, http=http,
                         upload_retry_delay=0).publish(card, "Пост")
    assert not result.ok and "413" in result.error
    assert calls.count("photos.getWallUploadServer") == 1


def test_vk_publish_does_not_retry_access_errors(tmp_path):
    card = tmp_path / "card.png"
    card.write_bytes(b"PNG")
    calls = []
    http = _flaky_upload_transport([15], calls)
    result = VkPublisher("T", -241020718, dry_run=False, http=http,
                         upload_retry_delay=0).publish(card, "Пост")
    assert not result.ok and "VK 15" in result.error
    assert calls.count("photos.getWallUploadServer") == 1


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


def _edit_transport(attachments, calls, *, get_by_id_error=False):
    """Стенд VK: wall.getById отдаёт вложения записи, wall.edit запоминается."""
    def handler(request):
        method = request.url.path.rsplit("/", 1)[-1]
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        calls.append((method, form))
        if method == "wall.getById":
            if get_by_id_error:
                return httpx.Response(200, json={"error": {"error_code": 6, "error_msg": "Too many"}})
            return httpx.Response(200, json={"response": {"items": [{"attachments": attachments}]}})
        return httpx.Response(200, json={"response": 1})
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_vk_edit_keeps_attachments_by_passing_them_explicitly():
    """wall.edit без attachments стирает фото записи (проверено на боевой стене), поэтому
    текущие вложения читаются и передаются в правку явно."""
    calls = []
    http = _edit_transport([
        {"type": "photo", "photo": {"owner_id": -241020718, "id": 456, "access_key": "abc"}},
        {"type": "link", "link": {"url": "https://splithome.ru/product/x/"}},
    ], calls)
    result = VkPublisher("TOKEN", -241020718, dry_run=False, http=http).edit_text(
        19, "Обновлённый пост", publish_at=1787931000,
    )
    assert result.ok and result.post_id == 19
    assert [name for name, _ in calls] == ["wall.getById", "wall.edit"]
    assert calls[0][1]["posts"] == "-241020718_19"
    form = calls[1][1]
    assert form["owner_id"] == "-241020718" and form["post_id"] == "19"
    assert form["publish_date"] == "1787931000"
    assert form["message"] == "Обновлённый пост"
    assert form["attachments"] == "photo-241020718_456_abc,https://splithome.ru/product/x/"


def test_vk_edit_of_text_only_post_sends_no_attachments():
    calls = []
    http = _edit_transport([], calls)
    result = VkPublisher("TOKEN", -241020718, dry_run=False, http=http).edit_text(
        19, "Обновлённый пост")
    assert result.ok
    assert "attachments" not in calls[-1][1]


def test_vk_edit_is_refused_when_attachments_cannot_be_read():
    """Не вслепую: если вложения прочитать не удалось, запись не правится, иначе потеряется фото."""
    calls = []
    http = _edit_transport([], calls, get_by_id_error=True)
    result = VkPublisher("TOKEN", -241020718, dry_run=False, http=http).edit_text(
        19, "Обновлённый пост")
    assert not result.ok and "6" in (result.error or "")
    assert [name for name, _ in calls] == ["wall.getById"]


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
