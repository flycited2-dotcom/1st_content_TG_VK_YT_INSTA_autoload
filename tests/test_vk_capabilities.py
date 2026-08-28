from urllib.parse import parse_qs

import httpx

from content_factory.publish.vk_capabilities import (
    probe_native_photo,
    read_state,
    write_state,
)


def test_probe_group_wall_uses_group_id_and_never_exposes_token():
    def handler(request):
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        assert form["group_id"] == "241020718"
        assert form["access_token"] == "SECRET"
        return httpx.Response(200, json={"error": {
            "error_code": 27, "error_msg": "Group authorization failed",
        }})

    capability = probe_native_photo(
        "SECRET", -241020718,
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    assert not capability.native_photo_ready
    assert capability.error_code == 27
    assert "SECRET" not in str(capability.public_dict())


def test_probe_user_token_for_group_can_be_ready():
    def handler(request):
        return httpx.Response(200, json={"response": {"upload_url": "https://upload.vk/u"}})

    capability = probe_native_photo(
        "USER_TOKEN", -241020718,
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    assert capability.native_photo_ready
    assert capability.error_code is None


def test_capability_state_is_public_and_atomic(tmp_path):
    capability = probe_native_photo("", -1)
    path = tmp_path / "vk.json"
    write_state(path, capability)
    assert read_state(path)["token_present"] is False
    assert not (tmp_path / "vk.json.tmp").exists()


def test_probe_explains_which_scopes_are_missing_behind_error_27():
    # Голая ошибка 27 не объясняет причину: у ключа сообщества права photos/wall
    # в маске есть, но методы загрузки закрыты для group auth целиком.
    def handler(request):
        return httpx.Response(200, json={"error": {
            "error_code": 27, "error_msg": "Group authorization failed",
        }})

    capability = probe_native_photo(
        "TOKEN", -241020718, granted_scope="vkid.personal_info offline",
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    assert list(capability.missing_scopes) == ["photos", "wall", "groups"]
    assert "photos" in (capability.error_message or "")


def test_probe_with_full_scope_reports_no_gap():
    def handler(request):
        return httpx.Response(200, json={"response": {"upload_url": "https://upload.vk/u"}})

    capability = probe_native_photo(
        "USER_TOKEN", -241020718,
        granted_scope="vkid.personal_info offline wall photos groups",
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    assert capability.native_photo_ready
    assert list(capability.missing_scopes) == []
