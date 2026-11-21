from __future__ import annotations

from gsuite2cpa.cleanup import run_delete


class FakeClient:
    def __init__(self, credentials):
        self.credentials = credentials
        self.deleted: list[str] = []
        self.disabled: list[str] = []

    def antigravity_credentials(self):
        return list(self.credentials)

    def delete_credential(self, name):
        self.deleted.append(name)

    def disable_credential(self, name, disabled=True):
        self.disabled.append(name)

    def download_credential(self, name):
        return b"{}"

    def verify_credential(self, name, model=None):
        return {"status": "ok", "verified": True}

    def reset_quota(self, index):
        return {"status": "ok"}


def credential(**overrides):
    data = {
        "name": "antigravity-user@example.com.json",
        "email": "user@example.com",
        "provider": "antigravity",
        "status": "active",
        "requires_sign_in": True,
    }
    data.update(overrides)
    return data


def test_dry_run_does_not_delete():
    client = FakeClient([credential()])
    code = run_delete(client, dry_run=True, use_quota=False)
    assert code == 0
    assert client.deleted == []
    assert client.disabled == []


def test_delete_removes_candidates_with_yes():
    client = FakeClient([credential()])
    code = run_delete(client, yes=True, use_quota=False)
    assert code == 0
    assert client.deleted == ["antigravity-user@example.com.json"]


def test_disable_mode():
    client = FakeClient([credential()])
    code = run_delete(client, yes=True, use_quota=False, disable_instead=True)
    assert code == 0
    assert client.disabled == ["antigravity-user@example.com.json"]


def test_healthy_credentials_are_kept():
    client = FakeClient([credential(requires_sign_in=False, status="active")])
    code = run_delete(client, yes=True, use_quota=False)
    assert code == 0
    assert client.deleted == []
