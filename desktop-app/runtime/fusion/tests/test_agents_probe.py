"""Local fixtures only: never run installed agents or a model."""
import json
import os
from pathlib import Path
import time

import pytest

from fusion import agents_probe


@pytest.fixture
def environment(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    binaries = tmp_path / "bin"
    binaries.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", str(binaries))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    for keys in agents_probe.ENV_KEYS.values():
        for key in keys:
            monkeypatch.delenv(key, raising=False)
    return home, binaries, tmp_path / "instance"


def cli(binaries, name, body="echo fixture-1.2"):
    path = binaries / name
    path.write_text("#!/bin/sh\n" + body + "\n")
    path.chmod(0o700)
    return path


def test_facts_only_and_no_secret(environment, monkeypatch):
    home, binaries, instance = environment
    cli(binaries, "codex")
    (home / ".codex").mkdir()
    (home / ".codex/config.toml").write_text(
        'model = "configured-model"\n[model_providers.penguinapi]\nenv_key = "FIXTURE_CREDENTIAL"\n'
        'base_url = "https://never-called.invalid"\napi_key = "inline-not-to-copy"\n')
    monkeypatch.setenv("FIXTURE_CREDENTIAL", "sensitive-fixture-value")
    (home / ".hermes/profiles/work").mkdir(parents=True)
    (home / ".pi").mkdir()
    facts = agents_probe.probe(instance)
    assert facts["codex"]["path"] == str(binaries / "codex")
    assert facts["codex"]["version"] == "fixture-1.2"
    assert facts["codex"]["model"] == "configured-model"
    assert facts["codex"]["model_providers"] == ["penguinapi"]
    assert facts["codex"]["env_key"] is True
    assert facts["hermes"]["profiles"] == ["work"]
    assert facts["pi"]["config_exists"] is True
    assert facts["pi"]["found"] is False
    assert facts["api"]["env_key"] is True
    assert facts["api"]["providers"] == [{"provider": "penguinapi", "env_key": True}]
    output = json.dumps(facts) + (instance / "agents-cache.json").read_text()
    for forbidden in ("sensitive-fixture-value", "inline-not-to-copy", "base_url", "capability", "擅长"):
        assert forbidden not in output


def test_version_timeout_all_eight_parallel_under_twenty_seconds(environment):
    _, binaries, instance = environment
    for name in agents_probe.CANDIDATES:
        cli(binaries, name, "/bin/sleep 60")
    started = time.monotonic()
    facts = agents_probe.probe(instance)
    elapsed = time.monotonic() - started
    assert 5 <= elapsed < 20
    assert all(facts[name]["version"] == "timeout" for name in agents_probe.CANDIDATES)


def test_version_descendant_does_not_hold_output_open(environment):
    _, binaries, instance = environment
    cli(binaries, "hermes", "/bin/sleep 60 &\necho wrapper-version")
    started = time.monotonic()
    facts = agents_probe.probe(instance)
    assert time.monotonic() - started < 5
    assert facts["hermes"]["version"] == "wrapper-version"


def test_cache_mtime_and_expiry(environment, monkeypatch):
    _, binaries, instance = environment
    cli(binaries, "codex")
    agents_probe.probe(instance)
    cache = instance / "agents-cache.json"
    original = agents_probe._version
    calls = []
    monkeypatch.setattr(agents_probe, "_version", lambda *args: calls.append(args) or "new-version")
    fresh = time.time() - 23 * 3600
    os.utime(cache, (fresh, fresh))
    assert agents_probe.probe(instance)["codex"]["version"] == "fixture-1.2"
    assert calls == []
    expired = time.time() - 25 * 3600
    os.utime(cache, (expired, expired))
    assert agents_probe.probe(instance)["codex"]["version"] == "new-version"
    assert len(calls) == 1
    monkeypatch.setattr(agents_probe, "_version", original)
    assert cache.stat().st_mode & 0o777 == 0o600


def test_cache_refresh_corruption_and_context_change(environment, monkeypatch):
    _, binaries, instance = environment
    cli(binaries, "pi")
    agents_probe.probe(instance)
    calls = []
    monkeypatch.setattr(agents_probe, "_version", lambda *args: calls.append(args) or "new")
    agents_probe.probe(instance, refresh=True)
    assert len(calls) == 1
    (instance / "agents-cache.json").write_text("invalid-json")
    agents_probe.probe(instance)
    assert len(calls) == 2
    monkeypatch.setenv("PATH", str(binaries) + ":/nonexistent")
    agents_probe.probe(instance)
    assert len(calls) == 3


def test_secret_echo_and_environment_are_sanitized(environment, monkeypatch):
    _, binaries, instance = environment
    monkeypatch.setenv("OPENAI_API_KEY", "fake-secret-value")
    cli(binaries, "codex", 'echo "fake-secret-value $OPENAI_API_KEY"')
    facts = agents_probe.probe(instance)
    assert facts["codex"]["env_key"] is True
    assert "fake-secret-value" not in json.dumps(facts)
    assert "fake-secret-value" not in (instance / "agents-cache.json").read_text()
    monkeypatch.setenv("OPENAI_API_KEY", "")
    assert agents_probe.probe(instance)["codex"]["env_key"] is False


@pytest.mark.parametrize("config", ['not valid TOML =', 'model = 123\nmodel_providers = 3'])
def test_bad_config_is_not_echoed(environment, config):
    home, _, instance = environment
    (home / ".codex").mkdir()
    (home / ".codex/config.toml").write_text(config)
    facts = agents_probe.probe(instance)
    assert facts["codex"]["model"] is None
    assert facts["api"]["found"] is False
