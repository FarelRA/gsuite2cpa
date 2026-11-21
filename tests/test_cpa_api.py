from __future__ import annotations

import pytest
import responses

from gsuite2cpa.cpa_api import CpaClient
from gsuite2cpa.errors import ApiError, AuthError

BASE = "http://cpa.test"


def make_client() -> CpaClient:
    return CpaClient(BASE, "test-key")


@responses.activate
def test_detect_v8_and_ping():
    responses.add(
        responses.GET,
        f"{BASE}/v8/management/credentials",
        json={"files": []},
        status=200,
    )
    client = make_client()
    assert client.detect_version() == "v8"
    health = client.ping()
    assert health == {"url": BASE, "api_version": "v8", "credentials": 0}
    client.close()


@responses.activate
def test_detect_v0_fallback_and_oauth():
    responses.add(responses.GET, f"{BASE}/v8/management/credentials", status=404)
    responses.add(responses.GET, f"{BASE}/v0/management/auth-files", json={"files": []})
    responses.add(
        responses.GET,
        f"{BASE}/v0/management/antigravity-auth-url",
        json={"status": "ok", "url": "https://accounts.google.com/auth", "state": "state-1"},
    )

    client = make_client()
    assert client.detect_version() == "v0"
    session = client.start_oauth()
    assert session["state"] == "state-1"
    assert session["url"].startswith("https://accounts.google.com")
    client.close()


@responses.activate
def test_start_oauth_v8():
    responses.add(responses.GET, f"{BASE}/v8/management/credentials", json={"files": []})
    responses.add(
        responses.GET,
        f"{BASE}/v8/management/oauth/auth-url",
        json={"status": "ok", "url": "https://accounts.google.com/o/oauth2", "state": "abc"},
    )
    client = make_client()
    session = client.start_oauth()
    assert session == {
        "url": "https://accounts.google.com/o/oauth2",
        "state": "abc",
    }
    client.close()


@responses.activate
def test_start_oauth_with_session_proxy_uses_post():
    responses.add(responses.GET, f"{BASE}/v8/management/credentials", json={"files": []})
    responses.add(
        responses.POST,
        f"{BASE}/v8/management/oauth/auth-url",
        json={"status": "ok", "url": "https://accounts.google.com/o/oauth2", "state": "abc"},
    )
    client = make_client()
    session = client.start_oauth(proxy_url="http://proxy:8080")
    assert session["state"] == "abc"
    request = responses.calls[-1].request
    assert request.method == "POST"
    assert b"proxy_url" in request.body
    client.close()


@responses.activate
def test_oauth_callback_and_status():
    responses.add(responses.GET, f"{BASE}/v8/management/credentials", json={"files": []})
    responses.add(responses.POST, f"{BASE}/v8/management/oauth/callback", json={"status": "ok"})
    responses.add(
        responses.GET,
        f"{BASE}/v8/management/oauth/status",
        json={"status": "ok", "auth_id": "antigravity-a@example.com.json", "saved": True},
    )
    client = make_client()
    client.submit_callback("abc", "code-123")
    result = client.wait_for_oauth("abc", timeout=2)
    assert result["auth_id"] == "antigravity-a@example.com.json"
    client.close()


@responses.activate
def test_oauth_callback_409_is_treated_as_already_completing():
    responses.add(responses.GET, f"{BASE}/v8/management/credentials", json={"files": []})
    responses.add(responses.POST, f"{BASE}/v8/management/oauth/callback", status=409)
    client = make_client()
    client.submit_callback("abc", "code-123")
    client.close()


@responses.activate
def test_refresh_all_and_verify():
    responses.add(responses.GET, f"{BASE}/v8/management/credentials", json={"files": []})
    responses.add(responses.POST, f"{BASE}/v8/management/credentials/refresh", json={"status": "ok"})
    responses.add(
        responses.POST,
        f"{BASE}/v8/management/credentials/verify",
        json={"status": "ok", "verified": True},
    )
    client = make_client()
    assert client.refresh_all()["status"] == "ok"
    result = client.verify_credential("antigravity-a.json", "gemini")
    assert result["verified"] is True
    client.close()


@responses.activate
def test_cancel_oauth_session():
    responses.add(responses.GET, f"{BASE}/v8/management/credentials", json={"files": []})
    responses.add(responses.DELETE, f"{BASE}/v8/management/oauth/session", json={"status": "ok"})
    client = make_client()
    client.cancel_oauth("abc")
    client.close()


@responses.activate
def test_oauth_status_error_raises_auth_error():
    responses.add(responses.GET, f"{BASE}/v8/management/credentials", json={"files": []})
    responses.add(
        responses.GET,
        f"{BASE}/v8/management/oauth/status",
        json={"status": "error", "error": "provider rejected"},
    )
    client = make_client()
    with pytest.raises(AuthError, match="provider rejected"):
        client.wait_for_oauth("abc", timeout=2)
    client.close()


@responses.activate
def test_list_credentials_paginates():
    responses.add(responses.GET, f"{BASE}/v8/management/credentials", json={"files": []})
    responses.add(
        responses.GET,
        f"{BASE}/v8/management/credentials",
        json={"files": [{"name": "antigravity-a.json", "provider": "antigravity"}], "has_more": True},
    )
    responses.add(
        responses.GET,
        f"{BASE}/v8/management/credentials",
        json={"files": [{"name": "antigravity-b.json", "provider": "antigravity"}], "has_more": False},
    )
    client = make_client()
    files = client.list_credentials()
    assert [item["name"] for item in files] == [
        "antigravity-a.json",
        "antigravity-b.json",
    ]
    client.close()


@responses.activate
def test_delete_and_disable():
    responses.add(responses.GET, f"{BASE}/v8/management/credentials", json={"files": []})
    responses.add(responses.DELETE, f"{BASE}/v8/management/credentials", json={"status": "ok"})
    responses.add(responses.PATCH, f"{BASE}/v8/management/credentials/status", json={"status": "ok"})
    client = make_client()
    client.delete_credential("antigravity-a.json")
    client.disable_credential("antigravity-a.json")
    client.close()


@responses.activate
def test_api_call():
    responses.add(responses.GET, f"{BASE}/v8/management/credentials", json={"files": []})
    responses.add(
        responses.POST,
        f"{BASE}/v8/management/requests/api-call",
        json={"status_code": 200, "header": {}, "body": "{}"},
    )
    client = make_client()
    result = client.api_call(
        auth_index="1",
        method="POST",
        url="https://example.test/quota",
        headers={"Authorization": "Bearer $TOKEN$"},
        data="{}",
    )
    assert result["status_code"] == 200
    client.close()


@responses.activate
def test_invalid_key_message_includes_hint():
    responses.add(responses.GET, f"{BASE}/v8/management/credentials", status=401, json={"error": "unauthorized"})
    client = make_client()
    with pytest.raises(ApiError, match="secret-key"):
        client.detect_version()
    client.close()


@responses.activate
def test_connection_error_is_translated():
    client = CpaClient("http://127.0.0.1:1", "key", timeout=0.2)
    with pytest.raises(ApiError, match="Cannot connect"):
        client.detect_version()
    client.close()
