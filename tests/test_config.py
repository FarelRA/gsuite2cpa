from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from gsuite2cpa import config


def test_save_config_is_private_and_round_trips(tmp_path):
    path = tmp_path / "config.json"
    config.save_config(path, {"url": "http://cpa", "api_key": "secret"})
    loaded = config.load_config(path)
    assert loaded == {"url": "http://cpa", "api_key": "secret"}
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_load_config_invalid_json(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(config.ConfigError):
        config.load_config(path)


def test_precedence_cli_env_config(monkeypatch, tmp_path):
    path = tmp_path / "config.json"
    config.save_config(path, {"url": "http://from-config", "api_key": "config-key"})
    monkeypatch.setenv("GS2CPA_URL", "http://from-env")
    monkeypatch.setenv("GS2CPA_KEY", "env-key")

    settings, _ = config.load_settings(config_path=str(path))
    assert settings["url"] == "http://from-env"
    assert settings["api_key"] == "env-key"

    settings, _ = config.load_settings(
        url="http://from-cli", api_key="cli-key", config_path=str(path)
    )
    assert settings["url"] == "http://from-cli"
    assert settings["api_key"] == "cli-key"


def test_oauth_proxy_precedence(monkeypatch, tmp_path):
    path = tmp_path / "config.json"
    config.save_config(path, {"url": "http://cpa", "api_key": "k", "oauth_proxy": "http://cfg"})
    settings, _ = config.load_settings(config_path=str(path))
    assert settings["oauth_proxy"] == "http://cfg"

    monkeypatch.setenv("GS2CPA_OAUTH_PROXY", "http://env")
    settings, _ = config.load_settings(config_path=str(path))
    assert settings["oauth_proxy"] == "http://env"

    settings, _ = config.load_settings(oauth_proxy="http://cli", config_path=str(path))
    assert settings["oauth_proxy"] == "http://cli"


def test_normalize_url():
    assert config.normalize_url("127.0.0.1:8317") == "http://127.0.0.1:8317"
    assert config.normalize_url("https://cpa.example.com/") == "https://cpa.example.com"
    with pytest.raises(config.ConfigError):
        config.normalize_url("")


def test_mask_secret():
    assert config.mask_secret("") == "(not set)"
    assert config.mask_secret("abcd") == "****"
    assert config.mask_secret("abcdefghij").startswith("abcd")
    assert config.mask_secret("abcdefghij").endswith("ghij")


def test_find_config_prefers_project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    local = tmp_path / config.CONFIG_FILE
    local.write_text("{}", encoding="utf-8")
    assert config.find_config_path() == Path(local)


def test_global_config_path_uses_xdg(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert config.global_config_path() == tmp_path / "gsuite2cpa" / "config.json"
