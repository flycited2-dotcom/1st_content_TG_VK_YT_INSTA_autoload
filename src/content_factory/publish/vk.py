"""VK Wall API: адаптация поста, preview/dry-run и публикация фото на стену."""
from __future__ import annotations

import html
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlencode

import httpx

VK_API = "https://api.vk.com/method"
VK_MESSAGE_MAX = 4096
VK_SHARE = "https://vk.com/share.php"


class _PlainText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in {"br", "p", "div", "blockquote", "li"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"p", "div", "blockquote", "li"}:
            self.parts.append("\n")

    def handle_data(self, data):
        self.parts.append(data)


def adapt_vk_text(caption: str, max_chars: int = VK_MESSAGE_MAX) -> str:
    """Telegram HTML → читаемый VK plain text без изменения фактов и цены."""
    parser = _PlainText()
    parser.feed(caption or "")
    text = html.unescape("".join(parser.parts))
    # Публичный каталог иногда склеивает индекс модели и следующее русское слово:
    # `SRW-12000-Dоднофазный` → `SRW-12000-D однофазный`.
    text = re.sub(r"(?<=[A-Z0-9])(?=[а-яё])", " ", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text[:max_chars].rstrip()


def build_vk_share_url(*, url: str, title: str, description: str,
                       image_url: str = "") -> str:
    """Официальное окно ручной публикации VK без выдачи издательского токена."""
    if not url.startswith("https://"):
        raise ValueError("VK Share требует публичный HTTPS URL")
    # Карточку ссылки VK всегда собирает из Open Graph страницы `url`.
    # Параметр `image` ненадёжен и может быть проигнорирован, поэтому изображение,
    # заголовок и описание закрепляются на отдельной товарной share-странице.
    params = {"url": url, "comment": description}
    if image_url:
        if not image_url.startswith("https://"):
            raise ValueError("Изображение VK Share должно иметь публичный HTTPS URL")
    return f"{VK_SHARE}?{urlencode(params)}"


def build_stabilizer_vk_comment(message: str) -> str:
    """Развернуть краткую карточку стабилизатора в читаемый продающий текст VK."""
    source = adapt_vk_text(message)
    lines = [line.strip() for line in source.splitlines() if line.strip()]
    title = lines[0] if lines else "Стабилизатор напряжения"
    price = next((line for line in lines if "₽" in line), "Цена по запросу")
    return "\r\n".join([
        f"⚡ {title}",
        "",
        price,
        "",
        "Почему стоит выбрать:",
        "✅ Защищает технику при нестабильном напряжении",
        "✅ Мощность 12 000 ВА",
        "✅ Рабочий диапазон 130–270 В",
        "✅ Настенное размещение — не занимает место на полу",
        "✅ Цифровой контроль напряжения",
        "✅ Гарантия производителя 36 месяцев",
        "✅ Подберём мощность под вашу нагрузку",
        "",
        "🚚 Доставка по Крыму, Запорожской и Херсонской областям",
        "💳 Оплата при получении после подтверждения заказа",
        "📦 Наличие и срок поставки подтвердит менеджер",
        "",
        "📞 +7 978 579-29-95",
        "Напишите или позвоните — подберём модель под параметры вашей сети.",
    ])[:VK_MESSAGE_MAX].rstrip()


@dataclass
class VkPublishResult:
    ok: bool
    dry_run: bool = False
    post_id: int | None = None
    error: str | None = None
    payload: dict | None = None


class VkPublisher:
    def __init__(self, token: str, owner_id: int, *, api_version: str = "5.199",
                 dry_run: bool = True, http: httpx.Client | None = None):
        self.token = token
        self.owner_id = int(owner_id)
        self.api_version = api_version
        self.dry_run = dry_run
        self.http = http or httpx.Client(timeout=60, follow_redirects=True)

    def preview(self, image: str, caption: str) -> dict:
        return {"owner_id": self.owner_id, "message": adapt_vk_text(caption),
                "image": str(image), "dry_run": self.dry_run}

    def _api(self, method: str, data: dict) -> dict:
        payload = {**data, "access_token": self.token, "v": self.api_version}
        response = self.http.post(f"{VK_API}/{method}", data=payload)
        response.raise_for_status()
        body = response.json() or {}
        if body.get("error"):
            err = body["error"]
            raise RuntimeError(f"VK {err.get('error_code')}: {err.get('error_msg')}")
        return body.get("response")

    def publish(self, image: str, caption: str) -> VkPublishResult:
        preview = self.preview(image, caption)
        if self.dry_run:
            return VkPublishResult(ok=True, dry_run=True, payload=preview)
        if not self.token:
            return VkPublishResult(ok=False, error="VK_ACCESS_TOKEN не задан", payload=preview)
        path = Path(image)
        if not path.is_file():
            return VkPublishResult(ok=False, error=f"файл карточки не найден: {path}",
                                   payload=preview)
        try:
            # VK API 5.199 отклоняет photos.getWallUploadServer для ключей
            # сообщества с ошибкой 27, даже когда у ключа есть право photos.
            # Канал фотографий сообщений при этом доступен: сохраняем изображение
            # как photo сообщества и прикрепляем полученный media id к wall.post.
            is_group = self.owner_id < 0
            upload_method = (
                "photos.getMessagesUploadServer" if is_group
                else "photos.getWallUploadServer"
            )
            upload = self._api(upload_method, {})
            with path.open("rb") as fh:
                upload_response = self.http.post(
                    upload["upload_url"], files={"photo": (path.name, fh.read(), "image/png")})
            upload_response.raise_for_status()
            uploaded = upload_response.json()
            save_params = {k: uploaded[k] for k in ("server", "photo", "hash")}
            if is_group:
                save_method = "photos.saveMessagesPhoto"
            else:
                save_method = "photos.saveWallPhoto"
                save_params["user_id"] = abs(self.owner_id)
            saved = self._api(save_method, save_params)
            photo = saved[0]
            attachment = f"photo{photo['owner_id']}_{photo['id']}"
            if photo.get("access_key"):
                attachment += f"_{photo['access_key']}"
            post = self._api("wall.post", {
                "owner_id": self.owner_id,
                "from_group": 1 if is_group else 0,
                "message": preview["message"],
                "attachments": attachment,
            })
            return VkPublishResult(ok=True, post_id=int(post["post_id"]), payload=preview)
        except (OSError, KeyError, IndexError, ValueError, RuntimeError,
                httpx.HTTPError) as exc:
            return VkPublishResult(ok=False, error=str(exc), payload=preview)
