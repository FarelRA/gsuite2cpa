from __future__ import annotations

from argparse import Namespace
from pathlib import Path

import pytest

from gsuite2cpa.accounts import Account
from gsuite2cpa.cli import _attempt_account
from gsuite2cpa.errors import AuthError


class FakeClient:
    def __init__(self, status: dict | None = None, credentials: dict | None = None):
        self.status = status or {"status": "ok", "auth_id": "antigravity-user@example.com.json"}
        self.credentials = credentials or {}
        self.cancelled: list[str] = []
        self.submitted: list[tuple[str, str]] = []
        self.verified: list[str] = []
        self.oauth_proxy: str | None = None

    def start_oauth(self, *, proxy_url=None):
        self.oauth_proxy = proxy_url
        return {"url": "https://accounts.google.com/x", "state": "state-1"}

    def submit_callback(self, state, code):
        self.submitted.append((state, code))

    def wait_for_oauth(self, state, timeout):
        return self.status

    def cancel_oauth(self, state):
        self.cancelled.append(state)

    def get_credential(self, name):
        return self.credentials.get(name)

    def verify_credential(self, name, model=None):
        self.verified.append(name)
        return {"status": "ok", "verified": True}


def account(email: str = "user@example.com") -> Account:
    return Account(email=email, password="pass", totp_secret=None, raw=f"{email}|pass", line_number=1)


def args(**overrides) -> Namespace:
    data = {
        "fast": True,
        "login_timeout": 30,
        "browser_path": None,
        "browser_proxy": None,
        "headless": False,
        "strict_email": True,
        "verify": False,
        "verify_model": None,
    }
    data.update(overrides)
    return Namespace(**data)


def test_attempt_account_success(monkeypatch):
    client = FakeClient()
    monkeypatch.setattr("gsuite2cpa.cli.login", lambda *a, **k: type("R", (), {"code": "code-1"})())
    auth_id = _attempt_account(client, account(), args())
    assert auth_id == "antigravity-user@example.com.json"
    assert client.submitted == [("state-1", "code-1")]
    assert client.verified == []


def test_attempt_account_passes_oauth_proxy(monkeypatch):
    client = FakeClient()
    monkeypatch.setattr("gsuite2cpa.cli.login", lambda *a, **k: type("R", (), {"code": "code-1"})())
    _attempt_account(client, account(), args(oauth_proxy="http://proxy:8080"))
    assert client.oauth_proxy == "http://proxy:8080"


def test_attempt_account_verify(monkeypatch):
    client = FakeClient()
    monkeypatch.setattr("gsuite2cpa.cli.login", lambda *a, **k: type("R", (), {"code": "code-1"})())
    auth_id = _attempt_account(client, account(), args(verify=True, verify_model="gemini"))
    assert auth_id == "antigravity-user@example.com.json"
    assert client.verified == ["antigravity-user@example.com.json"]


def test_attempt_account_email_mismatch(monkeypatch):
    client = FakeClient(status={"status": "ok", "auth_id": "antigravity-other@example.com.json"})
    monkeypatch.setattr("gsuite2cpa.cli.login", lambda *a, **k: type("R", (), {"code": "code-1"})())
    with pytest.raises(AuthError, match="signed in as other@example.com"):
        _attempt_account(client, account(), args())


def test_attempt_account_cancels_when_browser_fails(monkeypatch):
    client = FakeClient()
    monkeypatch.setattr(
        "gsuite2cpa.cli.login", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    with pytest.raises(RuntimeError):
        _attempt_account(client, account(), args())
    assert client.cancelled == ["state-1"]


def test_attempt_account_uses_expected_file_when_auth_id_missing(monkeypatch):
    client = FakeClient(
        status={"status": "ok"},
        credentials={"antigravity-user@example.com.json": {"name": "antigravity-user@example.com.json"}},
    )
    monkeypatch.setattr("gsuite2cpa.cli.login", lambda *a, **k: type("R", (), {"code": "code-1"})())
    auth_id = _attempt_account(client, account(), args())
    assert auth_id == "antigravity-user@example.com.json"


def test_attempt_account_requires_auth_id(monkeypatch):
    client = FakeClient(status={"status": "ok"}, credentials={})
    monkeypatch.setattr("gsuite2cpa.cli.login", lambda *a, **k: type("R", (), {"code": "code-1"})())
    with pytest.raises(AuthError, match="did not return a credential name"):
        _attempt_account(client, account(), args())


def test_account_file_default_is_relative_to_cwd():
    # Import is exercised here to make sure the CLI entry point stays importable.
    from gsuite2cpa.cli import _choose_account_file

    assert Path(_choose_account_file(None)).name in {"accounts.txt", "akun.txt"}
