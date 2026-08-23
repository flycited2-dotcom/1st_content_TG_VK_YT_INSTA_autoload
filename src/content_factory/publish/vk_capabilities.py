"""Безопасная проверка готовности VK к нативной загрузке фотографий.

Проверка запрашивает только адрес сервера загрузки и не загружает файл, не
сохраняет фотографию и не создаёт запись на стене. Токен никогда не попадает в
отчёт или файл состояния.
"""
from __future__ import annotations

import argparse
import json
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import httpx
from decouple import config

from content_factory.publish.telegram import send_message


VK_API = "https://api.vk.com/method"


@dataclass(frozen=True)
class VkPhotoCapability:
    owner_id: int
    checked_at: int
    token_present: bool
    native_photo_ready: bool
    error_code: int | None = None
    error_message: str | None = None

    def public_dict(self) -> dict:
        return asdict(self)


def probe_native_photo(token: str, owner_id: int, *, api_version: str = "5.199",
                       http: httpx.Client | None = None) -> VkPhotoCapability:
    """Проверить фактический доступ к ``photos.getWallUploadServer``.

    Это более надёжно, чем угадывать тип токена по ``owner_id``: отрицательный ID
    описывает стену сообщества, но пользовательский токен администратора тоже
    публикует именно на такой стене.
    """
    now = int(time.time())
    if not token:
        return VkPhotoCapability(int(owner_id), now, False, False,
                                 error_message="VK_ACCESS_TOKEN не задан")
    client = http or httpx.Client(timeout=30, follow_redirects=True)
    data: dict[str, object] = {
        "access_token": token,
        "v": api_version,
    }
    if int(owner_id) < 0:
        data["group_id"] = abs(int(owner_id))
    else:
        data["user_id"] = int(owner_id)
    try:
        response = client.post(f"{VK_API}/photos.getWallUploadServer", data=data)
        response.raise_for_status()
        body = response.json() or {}
        if body.get("error"):
            error = body["error"]
            return VkPhotoCapability(
                int(owner_id), now, True, False,
                error_code=int(error.get("error_code", 0) or 0) or None,
                error_message=str(error.get("error_msg") or "VK API error")[:300],
            )
        ready = bool((body.get("response") or {}).get("upload_url"))
        return VkPhotoCapability(
            int(owner_id), now, True, ready,
            error_message=None if ready else "VK не вернул upload_url",
        )
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        return VkPhotoCapability(int(owner_id), now, True, False,
                                 error_message=str(exc)[:300])


def write_state(path: str | Path, capability: VkPhotoCapability) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(
        json.dumps(capability.public_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(target)


def read_state(path: str | Path) -> dict:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Проверка нативных фото VK без публикации")
    parser.add_argument("--owner-id", type=int,
                        default=int(os.getenv("VK_OWNER_ID", "-241020718")))
    parser.add_argument("--state", default=os.getenv(
        "VK_CAPABILITY_STATE", "/opt/content-factory-vk/state/vk-capabilities.json"))
    args = parser.parse_args(argv)
    previous = read_state(args.state)
    capability = probe_native_photo(
        config("VK_ACCESS_TOKEN", default=""), args.owner_id,
    )
    write_state(args.state, capability)

    became_ready = (
        capability.native_photo_ready
        and not bool(previous.get("native_photo_ready"))
    )
    if became_ready:
        send_message(
            config("TELEGRAM_BOT_TOKEN", default=""),
            config("TELEGRAM_REVIEW_CHANNEL_ID", default=""),
            "✅ VK разрешил нативную загрузку фотографий. Автоматический режим ещё "
            "не включён: сначала нужна одна контрольная публикация.",
        )
    print(json.dumps(capability.public_dict(), ensure_ascii=False))
    # Недоступность фото является ожидаемым состоянием и не должна ломать timer.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
