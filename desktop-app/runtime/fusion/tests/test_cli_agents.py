"""CLI adapters are tested with fake binaries; no provider call is made."""
import os
from pathlib import Path

import pytest

from fusion.agent_channels import resolve
from fusion.cli_agents import CLITransport, parse_output


def test_cli_resolution_and_event_parsers():
    assert resolve("gemini", "") == ("cli", None, None, "default")
    assert parse_output("opencode", '{"type":"text","part":{"text":"hello"}}\n') == "hello"
    assert parse_output("copilot", '{"type":"assistant.message","data":{"content":"hi"}}\n') == "hi"
    assert parse_output("gemini", '{"response":"{\\"ok\\":true}"}') == '{"ok":true}'


def test_cli_transport_runs_in_empty_workspace(tmp_path, monkeypatch):
    binary = tmp_path / "gemini"
    binary.write_text(
        "#!/bin/sh\n"
        "test -z \"$(ls -A .)\" || exit 9\n"
        "printf '%s' '{\"response\":\"{\\\"hello\\\":\\\"world\\\"}\"}'\n"
    )
    binary.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    transport = CLITransport("gemini")
    result = transport(
        {"messages": [{"role": "system", "content": "return json"},
                      {"role": "user", "content": "hello"}]},
        tmp_path / "01-taizi.sse",
    )
    assert result["output"] == '{"hello":"world"}'
    assert result["model"] == "default"
    assert not (tmp_path / "01-taizi-cli").exists()


def test_role_binding_rejects_cli_gongbu(tmp_path, monkeypatch):
    from fusion.role_binding import ROLES, save
    binary = tmp_path / "gemini"
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o755)
    instance = tmp_path / "instance"
    instance.mkdir()
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    binding = {"mode": "single",
               "bindings": {role: {"agent": "gemini", "model": "default"} for role in ROLES}}
    with pytest.raises(Exception, match="工部"):
        save(str(instance), binding)
