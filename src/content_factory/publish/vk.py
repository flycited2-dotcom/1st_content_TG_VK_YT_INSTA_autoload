"""VK Wall API: адаптация поста, preview/dry-run и публикация фото на стену."""
from __future__ import annotations

import html
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path

import httpx

from content_factory.publish.vk_oauth import VkOAuthClient, VkOAuthSettings, VkTokenStore

VK_API = "https://api.vk.com/method"
VK_MESSAGE_MAX = 4096


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

    @classmethod
    def from_oauth_store(cls, *, app_id: int, redirect_uri: str, token_store: str,
                         owner_id: int, api_version: str = "5.199",
                         dry_run: bool = True, http: httpx.Client | None = None):
        """Создать издателя с автоматическим обновлением часового VK ID токена."""
        oauth = VkOAuthClient(VkOAuthSettings(app_id, redirect_uri), http=http)
        token = VkTokenStore(token_store).access_token(oauth)
        return cls(token, owner_id, api_version=api_version, dry_run=dry_run, http=http)

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
            upload_params = ({"group_id": abs(self.owner_id)} if self.owner_id < 0 else {})
            upload = self._api("photos.getWallUploadServer", upload_params)
            with path.open("rb") as fh:
                upload_response = self.http.post(
                    upload["upload_url"], files={"photo": (path.name, fh.read(), "image/png")})
            upload_response.raise_for_status()
            uploaded = upload_response.json()
            save_params = {k: uploaded[k] for k in ("server", "photo", "hash")}
            save_params["group_id" if self.owner_id < 0 else "user_id"] = abs(self.owner_id)
            saved = self._api("photos.saveWallPhoto", save_params)
            photo = saved[0]
            post = self._api("wall.post", {
                "owner_id": self.owner_id,
                "from_group": 1 if self.owner_id < 0 else 0,
                "message": preview["message"],
                "attachments": f"photo{photo['owner_id']}_{photo['id']}",
            })
            return VkPublishResult(ok=True, post_id=int(post["post_id"]), payload=preview)
        except (OSError, KeyError, IndexError, ValueError, RuntimeError,
                httpx.HTTPError) as exc:
            return VkPublishResult(ok=False, error=str(exc), payload=preview)
