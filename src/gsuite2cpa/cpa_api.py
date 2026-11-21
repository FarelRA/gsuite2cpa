"""CLIProxyAPI management client.

The client prefers the maintained ``/v8/management`` API (CLIProxyAPI v8+)
and transparently falls back to the legacy ``/v0/management`` API for the
core login/list/delete operations. Newer-only features raise
``UnsupportedFeatureError`` when connected to an old server.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterable
from typing import Any

import requests

from . import __version__
from .errors import ApiError, AuthError, UnsupportedFeatureError

USER_AGENT = f"gsuite2cpa/{__version__} (+https://github.com/mhiqrambg/gsuite2cpa)"
RETRY_STATUS = {429, 502, 503, 504}


class CpaClient:
    """Small, typed wrapper around CLIProxyAPI's management endpoints."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        proxy: str | None = None,
        insecure: bool = False,
        timeout: float = 30.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.version: str | None = None

        self.session = requests.Session()
        self.session.headers.update(
            {
                "Accept": "application/json",
                "User-Agent": USER_AGENT,
                "X-Management-Key": api_key,
            }
        )
        if api_key:
            self.session.headers["Authorization"] = f"Bearer {api_key}"
        if proxy:
            self.session.proxies.update({"http": proxy, "https": proxy})
        if insecure:
            self.session.verify = False
            try:
                import urllib3

                urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            except Exception:  # pragma: no cover - urllib3 is a requests dependency
                pass

    # ------------------------------------------------------------------
    # HTTP plumbing
    # ------------------------------------------------------------------
    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def _http(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        headers: dict[str, str] | None = None,
        safe: bool = True,
        attempts: int = 2,
    ) -> requests.Response:
        url = self._url(path)
        last_error: Exception | None = None
        for attempt in range(attempts):
            try:
                response = self.session.request(
                    method,
                    url,
                    params=params,
                    json=json_body,
                    headers=headers,
                    timeout=self.timeout,
                )
            except requests.exceptions.SSLError as exc:
                raise ApiError(
                    f"TLS verification failed for {url}. "
                    "Use --insecure only for a self-signed certificate.",
                    url=url,
                ) from exc
            except requests.exceptions.ConnectionError as exc:
                raise ApiError(
                    f"Cannot connect to CLIProxyAPI at {self.base_url}. "
                    "Check the URL and that the server is running.",
                    url=url,
                ) from exc
            except requests.exceptions.Timeout as exc:
                raise ApiError(
                    f"CLIProxyAPI did not respond within {self.timeout:g}s ({url}).",
                    url=url,
                ) from exc

            if safe and response.status_code in RETRY_STATUS and attempt + 1 < attempts:
                last_error = ApiError(
                    f"HTTP {response.status_code}", status=response.status_code, url=url
                )
                time.sleep(0.5 * (attempt + 1))
                continue
            return response
        raise last_error or ApiError(f"Request failed: {url}", url=url)

    @staticmethod
    def _payload(response: requests.Response) -> Any:
        text = response.text
        if not text:
            return None
        try:
            return response.json()
        except ValueError:
            return text.strip()

    def _error(self, method: str, path: str, response: requests.Response) -> ApiError:
        payload = self._payload(response)
        if isinstance(payload, dict):
            detail = payload.get("error") or payload.get("message") or payload
        else:
            detail = payload
        detail_text = str(detail).strip()
        if len(detail_text) > 400:
            detail_text = detail_text[:400] + "..."
        message = f"{method} {path} failed: HTTP {response.status_code}"
        if detail_text:
            message += f" — {detail_text}"

        if response.status_code in (401, 403):
            message += (
                " (check that --key matches remote-management.secret-key; "
                "remote URLs also need remote-management.allow-remote: true)"
            )
        return ApiError(
            message,
            status=response.status_code,
            payload=payload,
            url=self._url(path),
        )

    def _json(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        allow_404: bool = False,
        safe: bool = True,
    ) -> Any:
        response = self._http(
            method, path, params=params, json_body=json_body, safe=safe
        )
        if allow_404 and response.status_code == 404:
            return None
        if not 200 <= response.status_code < 300:
            raise self._error(method, path, response)
        return self._payload(response)

    # ------------------------------------------------------------------
    # Version detection
    # ------------------------------------------------------------------
    def detect_version(self, *, force: bool = False) -> str:
        """Detect whether the server exposes the v8 or legacy v0 API."""
        if self.version and not force:
            return self.version

        if not self.api_key:
            raise ApiError(
                "A CLIProxyAPI management key is required. "
                "Set remote-management.secret-key in CPA, then run "
                "`gsuite2cpa init --key <key>`."
            )

        v8 = self._http(
            "GET",
            "/v8/management/credentials",
            params={"page": 1, "page_size": 1},
            attempts=1,
        )
        if v8.status_code == 404:
            v0 = self._http("GET", "/v0/management/auth-files", attempts=1)
            if 200 <= v0.status_code < 300:
                self.version = "v0"
                return self.version
            raise self._error("GET", "/v0/management/auth-files", v0)
        if not 200 <= v8.status_code < 300:
            raise self._error("GET", "/v8/management/credentials", v8)

        self.version = "v8"
        return self.version

    def _path(self, v8_path: str, v0_path: str) -> str:
        version = self.detect_version()
        return v8_path if version == "v8" else v0_path

    # ------------------------------------------------------------------
    # Health and credentials
    # ------------------------------------------------------------------
    def ping(self) -> dict[str, Any]:
        version = self.detect_version(force=True)
        credentials = self.list_credentials()
        return {
            "url": self.base_url,
            "api_version": version,
            "credentials": len(credentials),
        }

    def list_credentials(self, *, max_pages: int = 100) -> list[dict[str, Any]]:
        """Return every credential visible to the management API."""
        version = self.detect_version()
        files: list[dict[str, Any]] = []
        page = 1
        while page <= max_pages:
            path = (
                "/v8/management/credentials"
                if version == "v8"
                else "/v0/management/auth-files"
            )
            params = {"page": page, "page_size": 200} if version == "v8" else None
            data = self._json("GET", path, params=params)
            if isinstance(data, list):
                files.extend(item for item in data if isinstance(item, dict))
                break
            if not isinstance(data, dict):
                break
            batch = data.get("files") or data.get("data") or []
            if isinstance(batch, dict):
                batch = batch.get("files") or []
            files.extend(item for item in batch if isinstance(item, dict))
            if version != "v8" or not data.get("has_more"):
                break
            page += 1
        return files

    def antigravity_credentials(self) -> list[dict[str, Any]]:
        result = []
        for cred in self.list_credentials():
            provider = str(cred.get("provider") or cred.get("type") or "").lower()
            if provider in {"antigravity", "anti-gravity", "ag"}:
                result.append(cred)
        return result

    def get_credential(self, name: str) -> dict[str, Any] | None:
        for cred in self.list_credentials():
            if cred.get("name") == name or cred.get("id") == name:
                return cred
        return None

    def delete_credential(self, name: str) -> None:
        path = self._path(
            "/v8/management/credentials", "/v0/management/auth-files"
        )
        self._json("DELETE", path, params={"name": name})

    def disable_credential(self, name: str, disabled: bool = True) -> None:
        path = self._path(
            "/v8/management/credentials/status",
            "/v0/management/auth-files/status",
        )
        self._json(
            "PATCH",
            path,
            json_body={"name": name, "disabled": bool(disabled)},
            safe=False,
        )

    def refresh_credential(self, name: str) -> dict[str, Any] | None:
        path = self._path(
            "/v8/management/credentials/refresh",
            "/v0/management/auth-files/refresh",
        )
        data = self._json(
            "POST", path, json_body={"name": name}, safe=False
        )
        return data if isinstance(data, dict) else None

    def verify_credential(self, name: str, model: str | None = None) -> dict[str, Any]:
        if self.detect_version() != "v8":
            raise UnsupportedFeatureError(
                "Credential verification requires CLIProxyAPI v8+ "
                "(POST /v8/management/credentials/verify)."
            )
        body: dict[str, Any] = {"name": name}
        if model:
            body["model"] = model
        data = self._json(
            "POST", "/v8/management/credentials/verify", json_body=body, safe=False
        )
        return data if isinstance(data, dict) else {}

    def download_credential(self, name: str) -> bytes:
        path = self._path(
            "/v8/management/credentials/download",
            "/v0/management/auth-files/download",
        )
        response = self._http("GET", path, params={"name": name})
        if not 200 <= response.status_code < 300:
            raise self._error("GET", path, response)
        return response.content

    def reset_quota(self, auth_index: str) -> dict[str, Any]:
        path = self._path(
            "/v8/management/routing/cooldown/reset", "/v0/management/reset-quota"
        )
        data = self._json(
            "POST", path, json_body={"auth_index": auth_index}, safe=False
        )
        return data if isinstance(data, dict) else {}

    # ------------------------------------------------------------------
    # OAuth
    # ------------------------------------------------------------------
    def start_oauth(
        self, provider: str = "antigravity", *, proxy_url: str | None = None
    ) -> dict[str, str]:
        version = self.detect_version()
        if version == "v8":
            path = "/v8/management/oauth/auth-url"
            if proxy_url:
                data = self._json(
                    "POST",
                    path,
                    params={"provider": provider},
                    json_body={"proxy_url": proxy_url},
                )
            else:
                data = self._json("GET", path, params={"provider": provider})
        else:
            if provider != "antigravity":
                raise UnsupportedFeatureError(
                    "The legacy v0 API only supports the Antigravity login flow."
                )
            if proxy_url:
                raise UnsupportedFeatureError(
                    "--oauth-proxy requires CLIProxyAPI v8+; the legacy v0 API "
                    "cannot set a per-login proxy."
                )
            path = "/v0/management/antigravity-auth-url"
            data = self._json("GET", path)

        if not isinstance(data, dict):
            raise AuthError(f"Unexpected OAuth start response: {data!r}")
        auth_url = data.get("url") or data.get("authUrl") or data.get("auth_url")
        state = data.get("state")
        if not auth_url or not state:
            raise AuthError(
                "CLIProxyAPI did not return an authorization URL and state. "
                f"Response: {data!r}"
            )
        return {"url": str(auth_url), "state": str(state)}

    def submit_callback(self, state: str, code: str, provider: str = "antigravity") -> None:
        path = self._path(
            "/v8/management/oauth/callback", "/v0/management/oauth-callback"
        )
        response = self._http(
            "POST",
            path,
            json_body={"provider": provider, "state": state, "code": code},
            safe=False,
        )
        if response.status_code == 409:
            # The browser may have hit a local forwarder first; the session is
            # already completing. Polling will report the final result.
            return
        if not 200 <= response.status_code < 300:
            raise self._error("POST", path, response)

    def oauth_status(self, state: str) -> dict[str, Any]:
        path = self._path(
            "/v8/management/oauth/status", "/v0/management/get-auth-status"
        )
        data = self._json("GET", path, params={"state": state})
        return data if isinstance(data, dict) else {"status": "ok"}

    def cancel_oauth(self, state: str) -> None:
        """Cancel a pending login session when a local attempt fails."""
        try:
            if self.detect_version() != "v8":
                return
            self._json(
                "DELETE",
                "/v8/management/oauth/session",
                params={"state": state},
                allow_404=True,
                safe=False,
            )
        except Exception:  # noqa: BLE001 - cleanup must never mask the real error
            pass

    def wait_for_oauth(self, state: str, *, timeout: float = 300.0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        interval = 0.75
        while time.monotonic() < deadline:
            data = self.oauth_status(state)
            status = str(data.get("status") or "").lower()
            if status == "ok":
                return data
            if status == "error":
                message = data.get("error") or "OAuth failed"
                raise AuthError(str(message))
            time.sleep(interval)
            interval = min(interval * 1.25, 2.5)
        raise AuthError(
            "Timed out waiting for CLIProxyAPI to finish the OAuth exchange. "
            "The credential may still be saved; run `gsuite2cpa list` to check."
        )

    # ------------------------------------------------------------------
    # Generic authenticated upstream call (used for live quota checks)
    # ------------------------------------------------------------------
    def api_call(
        self,
        *,
        auth_index: str,
        method: str,
        url: str,
        headers: dict[str, str] | None = None,
        data: str | None = None,
        proxy_url: str | None = None,
    ) -> dict[str, Any]:
        path = self._path(
            "/v8/management/requests/api-call", "/v0/management/api-call"
        )
        body: dict[str, Any] = {
            "auth_index": auth_index,
            "method": method,
            "url": url,
            "header": headers or {},
        }
        if data is not None:
            body["data"] = data
        if proxy_url:
            body["proxy_url"] = proxy_url
        payload = self._json("POST", path, json_body=body, safe=False)
        if not isinstance(payload, dict):
            raise ApiError(f"Unexpected api-call response: {payload!r}")
        if "status_code" not in payload:
            raise ApiError(f"Unexpected api-call response: {payload!r}")
        return payload

    def close(self) -> None:
        self.session.close()

    def __enter__(self) -> CpaClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def summarize_credentials(credentials: Iterable[dict[str, Any]]) -> dict[str, int]:
    """Count credentials by status for human-readable summaries."""
    counts: dict[str, int] = {}
    for cred in credentials:
        status = str(cred.get("status") or "unknown").lower()
        counts[status] = counts.get(status, 0) + 1
    return counts


def credential_email(cred: dict[str, Any]) -> str | None:
    email = cred.get("email")
    if isinstance(email, str) and "@" in email:
        return email
    return None


def auth_index(cred: dict[str, Any]) -> str | None:
    value = cred.get("auth_index")
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
