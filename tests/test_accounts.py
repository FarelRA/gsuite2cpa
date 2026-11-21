from __future__ import annotations

import os
import stat

from gsuite2cpa.accounts import email_from_auth_id, read_accounts, remove_account


def test_read_accounts_skips_comments_and_invalid_lines(tmp_path):
    path = tmp_path / "accounts.txt"
    path.write_text(
        "# comment\n"
        "\n"
        "one@example.com|pass1\n"
        "bad-line\n"
        "two@example.com|pass2|JBSWY3DPEHPK3PXP\n"
        "one@example.com|duplicate\n",
        encoding="utf-8",
    )

    accounts = read_accounts(path)
    assert [account.email for account in accounts] == ["one@example.com", "two@example.com"]
    assert accounts[0].password == "pass1"
    assert accounts[0].totp_secret is None
    assert accounts[1].totp_secret == "JBSWY3DPEHPK3PXP"
    assert accounts[0].line_number == 3


def test_read_accounts_missing_file(tmp_path):
    assert read_accounts(tmp_path / "missing.txt") == []


def test_remove_account_is_atomic_and_keeps_mode(tmp_path):
    path = tmp_path / "accounts.txt"
    path.write_text("a@example.com|one\nb@example.com|two\n", encoding="utf-8")
    os.chmod(path, 0o640)

    assert remove_account(path, "a@example.com|one") is True
    assert path.read_text(encoding="utf-8") == "b@example.com|two\n"
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o640

    assert remove_account(path, "missing") is False


def test_email_from_auth_id():
    assert email_from_auth_id("antigravity-user@example.com.json") == "user@example.com"
    assert email_from_auth_id("user@example.com") == "user@example.com"
    assert email_from_auth_id("antigravity.json") is None
    assert email_from_auth_id(None) is None
