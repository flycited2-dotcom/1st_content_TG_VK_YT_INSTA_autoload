from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from content_factory.publish.vk_oauth import (
    PendingAuthorization,
    VkOAuthClient,
    VkOAuthSettings,
    VkTokenStore,
    code_challenge,
    missing_photo_scopes,
    resolve_publisher_token,
)


def test_begin_builds_pkce_authorization_url():
    client = VkOAuthClient(VkOAuthSettings(54732587, "https://climat-simf.ru/"))
    url, pending = client.begin()
    query = parse_qs(urlparse(url).query)
    assert query["client_id"] == ["54732587"]
    assert query["redirect_uri"] == ["https://climat-simf.ru/"]
    assert query["scope"] == ["vkid.personal_info offline wall photos groups"]
    assert query["state"] == [pending.state]
    assert query["code_challenge"] == [code_challenge(pending.code_verifier)]
    assert len(pending.code_verifier) >= 43


def test_callback_rejects_wrong_state():
    pending = PendingAuthorization("right", "v" * 64, 1)
    with pytest.raises(ValueError, match="state"):
        VkOAuthClient.parse_callback(
            "https://climat-simf.ru/?code=c&state=wrong&device_id=d", pending)


def test_exchange_keeps_tokens_out_of_url_and_returns_device_id():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["body"] = request.content.decode()
        return httpx.Response(200, json={
            "access_token": "access", "refresh_token": "refresh", "state": "s"
        })

    client = VkOAuthClient(
        VkOAuthSettings(54732587, "https://climat-simf.ru/"),
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    pending = PendingAuthorization("s", "v" * 64, 1)
    result = client.exchange(
        "https://climat-simf.ru/?code=secret-code&state=s&device_id=device", pending)
    assert result["device_id"] == "device"
    assert "secret-code" not in seen["url"]
    assert "code=secret-code" in seen["body"]


def test_token_store_round_trip(tmp_path):
    path = tmp_path / "tokens.json"
    store = VkTokenStore(path)
    store.save({"access_token": "a", "refresh_token": "r"})
    data = store.load()
    assert data["access_token"] == "a"
    assert data["refresh_token"] == "r"
    assert isinstance(data["saved_at"], int)


def test_token_store_refreshes_expired_token(tmp_path):
    path = tmp_path / "tokens.json"
    path.write_text('{"access_token":"old","refresh_token":"r","device_id":"d",'
                    '"expires_in":1,"saved_at":1}', encoding="utf-8")

    class OAuth:
        def refresh(self, refresh_token, device_id):
            assert (refresh_token, device_id) == ("r", "d")
            return {"access_token": "new", "refresh_token": "r2",
                    "device_id": "d", "expires_in": 3600}

    assert VkTokenStore(path).access_token(OAuth()) == "new"
    assert VkTokenStore(path).load()["refresh_token"] == "r2"


def test_missing_photo_scopes_names_exact_gap():
    # Именно это молча возвращает VK ID, если права приложения не согласованы.
    assert missing_photo_scopes("vkid.personal_info offline") == ("photos", "wall", "groups")
    assert missing_photo_scopes("vkid.personal_info offline wall photos groups") == ()


def test_resolve_publisher_token_prefers_user_token_over_group_key(tmp_path):
    path = tmp_path / "tokens.json"
    VkTokenStore(path).save({"access_token": "user", "refresh_token": "r",
                             "device_id": "d", "expires_in": 3600})
    assert resolve_publisher_token(store_path=path, env_token="group-key") == "user"


def test_resolve_publisher_token_refreshes_expired_user_token(tmp_path):
    # Токен VK ID живёт час, поэтому статичное значение в .env непригодно.
    path = tmp_path / "tokens.json"
    path.write_text('{"access_token":"old","refresh_token":"r","device_id":"d",'
                    '"expires_in":1,"saved_at":1}', encoding="utf-8")

    class OAuth:
        def refresh(self, refresh_token, device_id):
            assert (refresh_token, device_id) == ("r", "d")
            return {"access_token": "fresh", "refresh_token": "r2",
                    "device_id": "d", "expires_in": 3600}

    assert resolve_publisher_token(store_path=path, env_token="group-key",
                                   client=OAuth()) == "fresh"


def test_resolve_publisher_token_falls_back_to_group_key(tmp_path):
    # Без OAuth ключ сообщества остаётся рабочим для текстовых записей.
    assert resolve_publisher_token(store_path=tmp_path / "absent.json",
                                   env_token="group-key") == "group-key"


def test_resolve_publisher_token_falls_back_when_refresh_fails(tmp_path):
    path = tmp_path / "tokens.json"
    path.write_text('{"access_token":"old","refresh_token":"r","device_id":"d",'
                    '"expires_in":1,"saved_at":1}', encoding="utf-8")

    class OAuth:
        def refresh(self, refresh_token, device_id):
            raise RuntimeError("invalid_grant")

    assert resolve_publisher_token(store_path=path, env_token="group-key",
                                   client=OAuth()) == "group-key"
