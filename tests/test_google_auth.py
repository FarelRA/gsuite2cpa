from __future__ import annotations

import pytest

from gsuite2cpa.errors import AuthError
from gsuite2cpa.google_auth import extract_code_from_url


def test_extract_code():
    url = "http://localhost:51121/oauth-callback?code=abc123&state=xyz"
    assert extract_code_from_url(url, "xyz") == "abc123"


def test_extract_code_allows_missing_state():
    url = "http://localhost:51121/oauth-callback?code=abc123"
    assert extract_code_from_url(url) == "abc123"


def test_extract_code_rejects_state_mismatch():
    url = "http://localhost:51121/oauth-callback?code=abc&state=other"
    with pytest.raises(AuthError, match="different OAuth state"):
        extract_code_from_url(url, "expected")


def test_extract_code_surfaces_oauth_error():
    url = "http://localhost:51121/oauth-callback?error=access_denied&error_description=denied"
    with pytest.raises(AuthError, match="access_denied"):
        extract_code_from_url(url)


def test_extract_code_returns_none_for_unrelated_url():
    assert extract_code_from_url("https://accounts.google.com/signin") is None
    assert extract_code_from_url("") is None
