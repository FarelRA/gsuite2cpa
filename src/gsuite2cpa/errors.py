"""Errors shared by the CLI, API client, and browser automation."""

from __future__ import annotations

from typing import Any


class GSuite2CPAError(Exception):
    """Base class for all expected failures."""


class ConfigError(GSuite2CPAError):
    """The local configuration is missing or invalid."""


class ApiError(GSuite2CPAError):
    """CLIProxyAPI returned an error or could not be reached."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        payload: Any = None,
        url: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.payload = payload
        self.url = url


class AuthError(GSuite2CPAError):
    """The Google or CLIProxyAPI OAuth exchange failed."""


class BrowserError(GSuite2CPAError):
    """Chrome/Chromium automation failed."""


class LoginRequiredError(AuthError):
    """Google presented a challenge that cannot be automated safely."""


class UnsupportedFeatureError(GSuite2CPAError):
    """The connected CLIProxyAPI build does not expose a required endpoint."""
