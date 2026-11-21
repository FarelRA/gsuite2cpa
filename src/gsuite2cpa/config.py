"""Configuration, defaults, and timing profiles.

Configuration precedence (highest first):

1. command line flag
2. environment variable
3. config file
4. built-in default
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .errors import ConfigError

APP_NAME = "gsuite2cpa"
CONFIG_FILE = ".gs2cpa.json"
DEFAULT_ACCOUNT_FILE = "accounts.txt"
DEFAULT_URL = "http://127.0.0.1:8317"
DEFAULT_DELAY = 3.0
DEFAULT_OAUTH_TIMEOUT = 300.0
DEFAULT_HTTP_TIMEOUT = 30.0

# Environment aliases. The first name is the canonical one.
ENV_URL = ("GS2CPA_URL", "CLIPROXYAPI_URL", "CPA_URL")
ENV_KEY = (
    "GS2CPA_KEY",
    "CLIPROXYAPI_MANAGEMENT_KEY",
    "CLIPROXYAPI_KEY",
    "CPA_MANAGEMENT_KEY",
    "CPA_KEY",
)
ENV_PROXY = ("GS2CPA_PROXY", "CLIPROXYAPI_PROXY", "CPA_PROXY")
ENV_OAUTH_PROXY = ("GS2CPA_OAUTH_PROXY", "CLIPROXYAPI_OAUTH_PROXY")
ENV_INSECURE = ("GS2CPA_INSECURE", "CLIPROXYAPI_INSECURE")
ENV_BROWSER = ("GS2CPA_BROWSER", "CHROME_PATH")

# Timing profiles for the Google consent loop. The values are seconds.
TIMING: dict[str, dict[str, float]] = {
    "fast": {
        "page_load": 0.8,
        "after_email_input": 0.3,
        "after_email_next": 1.0,
        "password_timeout": 15.0,
        "after_password": 0.3,
        "after_password_next": 1.0,
        "consent_step_wait": 0.8,
        "after_consent_click": 1.0,
        "no_button_wait": 1.0,
        "redirect_wait": 1.5,
    },
    "normal": {
        "page_load": 2.0,
        "after_email_input": 0.8,
        "after_email_next": 2.0,
        "password_timeout": 20.0,
        "after_password": 0.8,
        "after_password_next": 2.0,
        "consent_step_wait": 1.0,
        "after_consent_click": 1.5,
        "no_button_wait": 2.0,
        "redirect_wait": 2.5,
    },
}

CHROME_ARGS = [
    "--start-maximized",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-extensions",
    "--disable-sync",
    "--disable-translate",
    "--disable-infobars",
    "--disable-blink-features=AutomationControlled",
    "--lang=en-US,en",
]

# Chrome's own automation switch. DrissionPage adds it; remove it to look less bot-like.
CHROME_IGNORE_DEFAULT_ARGS = ["--enable-automation"]


def global_config_path() -> Path:
    """Return the per-user config path."""
    if os.name == "nt":
        base = os.environ.get("APPDATA") or str(Path.home())
        return Path(base) / APP_NAME / "config.json"
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / APP_NAME / "config.json"


def find_config_path(explicit: str | None = None) -> Path:
    """Locate the active config file.

    An explicit ``--config`` path always wins. Otherwise a project-local config
    is preferred over the per-user config. The non-existent project path is
    returned as the default for ``init``.
    """
    if explicit:
        return Path(explicit).expanduser()

    local = Path.cwd() / CONFIG_FILE
    if local.exists():
        return local

    user = global_config_path()
    if user.exists():
        return user

    return local


def load_config(path: Path) -> dict[str, Any]:
    """Read a JSON config file. Missing files return an empty mapping."""
    if not path.exists():
        return {}
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"Cannot read config file {path}: {exc}") from exc
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"Invalid JSON in config file {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"Config file {path} must contain a JSON object")
    return data


def save_config(path: Path, data: dict[str, Any]) -> Path:
    """Atomically write the config file with owner-only permissions.

    Credentials live in this file, so never leave it world-readable.
    """
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, indent=2, sort_keys=True) + "\n"

    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        if os.name != "nt":
            os.chmod(tmp_path, stat.S_IRUSR | stat.S_IWUSR)
        os.replace(tmp_path, path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
    return path


def first_env(names: Iterable[str]) -> str | None:
    """Return the first non-empty environment variable from ``names``."""
    for name in names:
        value = os.environ.get(name)
        if value is not None and value.strip():
            return value.strip()
    return None


def env_bool(names: Iterable[str]) -> bool | None:
    value = first_env(names)
    if value is None:
        return None
    return value.lower() in {"1", "true", "yes", "on"}


def pick(
    cli_value: Any,
    env_names: Iterable[str],
    config: dict[str, Any],
    key: str,
    default: Any,
) -> Any:
    """Resolve a value using CLI > env > config > default."""
    if cli_value is not None:
        return cli_value
    env_value = first_env(env_names)
    if env_value is not None:
        return env_value
    if key in config and config[key] is not None:
        return config[key]
    return default


def normalize_url(value: str) -> str:
    value = (value or "").strip()
    if not value:
        raise ConfigError("CLIProxyAPI URL is empty")
    if "://" not in value:
        value = "http://" + value
    return value.rstrip("/")


def mask_secret(secret: str | None, visible: int = 4) -> str:
    """Mask a secret for terminal output."""
    if not secret:
        return "(not set)"
    if len(secret) <= visible * 2:
        return "*" * len(secret)
    return f"{secret[:visible]}{'*' * (len(secret) - visible * 2)}{secret[-visible:]}"


def load_settings(
    *,
    url: str | None = None,
    api_key: str | None = None,
    proxy: str | None = None,
    oauth_proxy: str | None = None,
    insecure: bool | None = None,
    browser_path: str | None = None,
    browser_proxy: str | None = None,
    config_path: str | None = None,
) -> tuple[dict[str, Any], Path]:
    """Resolve connection settings and return ``(settings, config_path)``."""
    path = find_config_path(config_path)
    config = load_config(path)

    resolved_url = normalize_url(pick(url, ENV_URL, config, "url", DEFAULT_URL))
    resolved_key = pick(api_key, ENV_KEY, config, "api_key", "") or ""
    resolved_proxy = pick(proxy, ENV_PROXY, config, "proxy", None) or None
    resolved_oauth_proxy = pick(
        oauth_proxy, ENV_OAUTH_PROXY, config, "oauth_proxy", None
    ) or None
    resolved_insecure = pick(insecure, ENV_INSECURE, config, "insecure", False)
    resolved_browser = pick(browser_path, ENV_BROWSER, config, "browser_path", None) or None
    resolved_browser_proxy = pick(
        browser_proxy, ("GS2CPA_BROWSER_PROXY",), config, "browser_proxy", None
    ) or resolved_proxy

    settings = {
        "url": resolved_url,
        "api_key": resolved_key,
        "proxy": resolved_proxy,
        "oauth_proxy": resolved_oauth_proxy,
        "insecure": bool(resolved_insecure),
        "browser_path": resolved_browser,
        "browser_proxy": resolved_browser_proxy,
    }
    return settings, path
