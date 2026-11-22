"""Live probe tests use a fake CLI and never call a provider."""
import os

from fusion.core import PolicyError
from fusion.live_probe import probe


def test_live_probe_fake_cli_and_cooldown(tmp_path, monkeypatch):
    binary = tmp_path / "gemini"
    binary.write_text("#!/bin/sh\nprintf OK\n")
    binary.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    result = probe("gemini", instance=tmp_path)
    assert result["ok"] is True
    assert result["reply"] == "OK"
    assert (tmp_path / "live-probe.json").stat().st_mode & 0o777 == 0o600
    try:
        probe("gemini", instance=tmp_path)
    except PolicyError as exc:
        assert "冷却" in str(exc)
    else:
        raise AssertionError("second probe was not rate limited")
