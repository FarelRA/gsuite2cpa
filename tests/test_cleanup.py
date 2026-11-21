from __future__ import annotations

import os
import stat
from datetime import datetime, timedelta, timezone

from gsuite2cpa.cleanup import _write_backup, classify_credential
from gsuite2cpa.quota import QuotaBucket, QuotaSnapshot

FUTURE = datetime.now(timezone.utc) + timedelta(hours=1)


def credential(**overrides):
    data = {
        "name": "antigravity-user@example.com.json",
        "email": "user@example.com",
        "provider": "antigravity",
        "status": "active",
    }
    data.update(overrides)
    return data


def test_terminal_sign_in_is_removed():
    report = classify_credential(credential(requires_sign_in=True))
    assert report.should_remove is True
    assert "sign-in required" in report.reasons


def test_terminal_error_message_is_removed():
    report = classify_credential(
        credential(status="error", status_message="invalid_grant: refresh token revoked")
    )
    assert report.should_remove is True


def test_generic_error_is_kept_by_default():
    report = classify_credential(credential(status="error", status_message="temporary upstream 500"))
    assert report.should_remove is False
    report = classify_credential(
        credential(status="error", status_message="temporary upstream 500"),
        include_error=True,
    )
    assert report.should_remove is True


def test_disabled_is_kept_unless_requested():
    assert classify_credential(credential(disabled=True)).should_remove is False
    assert (
        classify_credential(credential(disabled=True), include_disabled=True).should_remove
        is True
    )


def test_quota_blocked_and_exhausted_quotas_are_removed():
    assert classify_credential(credential(quota_blocked=True)).should_remove is True

    snapshot = QuotaSnapshot(
        buckets={
            "gemini_weekly": QuotaBucket(
                "gemini_weekly", "Gemini (Weekly)", "weekly", 0, FUTURE
            )
        }
    )
    report = classify_credential(credential(), quota=snapshot)
    assert report.should_remove is True
    assert "all weekly quota exhausted" in report.reasons


def test_successful_verification_overrides_stale_quota():
    snapshot = QuotaSnapshot(
        buckets={
            "gemini_weekly": QuotaBucket(
                "gemini_weekly", "Gemini (Weekly)", "weekly", 0, FUTURE
            )
        }
    )
    assert (
        classify_credential(credential(), quota=snapshot, verify_ok=True).should_remove
        is False
    )


def test_healthy_quota_is_kept():
    snapshot = QuotaSnapshot(
        buckets={
            "gemini_weekly": QuotaBucket(
                "gemini_weekly", "Gemini (Weekly)", "weekly", 0.8, FUTURE
            )
        }
    )
    assert classify_credential(credential(), quota=snapshot).should_remove is False


def test_verify_failure_is_removed():
    assert classify_credential(credential(), verify_ok=False).should_remove is True


def test_unavailable_only_with_flag():
    assert classify_credential(credential(unavailable=True)).should_remove is False
    assert (
        classify_credential(credential(unavailable=True), include_unavailable=True).should_remove
        is True
    )


class FakeClient:
    def __init__(self, payload: bytes):
        self.payload = payload

    def download_credential(self, name: str) -> bytes:
        return self.payload


def test_backup_is_private(tmp_path):
    saved = _write_backup(FakeClient(b'{"refresh_token":"x"}'), [classify_credential(credential())], tmp_path)
    path = saved["antigravity-user@example.com.json"]
    assert path.read_bytes() == b'{"refresh_token":"x"}'
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
