"""Readiness tests use fake binaries and never call a model provider."""
import os
from pathlib import Path

from fusion.agent_health import health


def _binary(tmp_path, name):
    path = tmp_path / name
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(0o755)
    return path


def test_cli_readiness_distinguishes_missing_login_and_key(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", str(tmp_path))
    for key in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "OPENAI_API_KEY", "PENGUIN_GPT_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    assert health("gemini")["state"] == "not_installed"
    _binary(tmp_path, "gemini")
    assert health("gemini")["state"] == "login_required"
    monkeypatch.setenv("GEMINI_API_KEY", "fixture")
    assert health("gemini")["state"] == "ready"
    assert health("gemini")["auth_mode"] == "api_key"


def test_cli_login_config_is_recognized(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".gemini").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    _binary(tmp_path, "gemini")
    result = health("gemini")
    assert result["state"] == "ready"
    assert result["auth_mode"] == "cli_login"
    assert result["verified"] is False


def test_api_channel_ready_is_separate_from_cli_install(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.delenv("PENGUIN_GPT_API_KEY", raising=False)
    assert health("codex")["state"] == "not_configured"
    monkeypatch.setenv("PENGUIN_GPT_API_KEY", "fixture")
    assert health("codex")["state"] == "ready"
    assert health("codex")["auth_mode"] == "api_key"
