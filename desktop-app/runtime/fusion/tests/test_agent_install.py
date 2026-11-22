"""Offline installer safety, preset binding and real HTTP session boundaries."""
import json
import os
from pathlib import Path
import time

import pytest

from fusion import agent_install, role_binding
from fusion.tests.test_auth import http, call


@pytest.fixture
def facts(monkeypatch):
    value = {"codex": {"found": False, "path": None}}
    monkeypatch.setattr(agent_install, "probe", lambda *a, **kw: value)
    return value


@pytest.fixture
def installer(tmp_path, facts):
    return agent_install.Installer(tmp_path / "instance")


ADMIN = {"name": "boss", "role": "admin"}


@pytest.fixture(autouse=True)
def channel_keys(monkeypatch):
    for key in ("PENGUIN_GPT_API_KEY", "MODELFLARE_CLAUDE_API_KEY", "DEEPSEEK_API_KEY"):
        monkeypatch.setenv(key, "TEST-NOT-A-SECRET")


def install(installer, agent="codex", session=ADMIN, **extra):
    return installer.handle("POST", "/api/agents/install",
                            json.dumps({"agent": agent, **extra}).encode(), session)


def finish(installer, job):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        code, value = installer.handle("GET", "/api/agents/install/" + job, b"", ADMIN)
        assert code == 200
        if value["status"] != "running":
            return value
        time.sleep(.01)
    pytest.fail("installer did not finish")


def npm(tmp_path, monkeypatch, script):
    executable = tmp_path / "bin" / "npm"
    executable.parent.mkdir()
    executable.write_text("#!/bin/sh\n" + script)
    executable.chmod(0o700)
    monkeypatch.setenv("PATH", str(executable.parent) + os.pathsep + os.defpath)
    return executable


def test_board_preset_and_partial_persistence(tmp_path):
    expected = {"taizi": ("hermes", "gpt-6.1-terra"), "zhongshu": ("codex", "gpt-6.1-sol"),
                "menxia": ("claude", "claude-sonnet-5-5"), "shangshu": ("pi", "deepseek-flash"),
                "gongbu": ("codex", "gpt-6.1-sol")}
    assert {r: (e["agent"], e["model"]) for r, e in role_binding.DEFAULT_PRESET.items()} == expected
    assert expected["menxia"][0] != expected["zhongshu"][0]
    facts = {"codex": {"found": True, "path": "/fixture/codex"}}
    role_binding.apply_preset({name: {"found": True} for name in agent_install.installability()}, tmp_path)
    result = role_binding.apply_preset(facts, tmp_path)
    assert set(result["bindings"]) == {"zhongshu", "gongbu"}
    assert set(result["missing"]) == {"taizi", "menxia", "shangshu"}
    assert role_binding.load(tmp_path)["bindings"] == result["bindings"]
    assert (tmp_path / "roles.json").stat().st_mode & 0o777 == 0o600
    assert all(row["path"] == "/fixture/codex" for row in role_binding.preset_status(facts) if row["found"])


def test_empty_preset_and_agent_default_model(tmp_path):
    assert role_binding.apply_preset({})["bindings"] == {}
    result = role_binding.apply_preset({"claude": {"found": True}}, tmp_path)
    assert result["bindings"]["menxia"]["model"] == "claude-sonnet-5-5"


@pytest.mark.parametrize("agent", ["unknown", "hermes", "codex;touch /tmp/forbidden", [], None])
def test_whitelist_rejects_without_spawn(installer, agent, monkeypatch):
    monkeypatch.setattr(agent_install.subprocess, "Popen", lambda *a, **kw: pytest.fail("spawned"))
    assert install(installer, agent)[0] == 400
    assert not agent_install.installability()["hermes"]["installable"]
    assert agent_install.HERMES_DOCS.startswith("https://hermes-agent.nousresearch.com/")


def test_reject_command_and_already_installed(installer, facts):
    assert install(installer, command="anything")[0] == 400
    facts["codex"]["found"] = True
    assert install(installer)[0] == 409
    records = [json.loads(line) for line in (installer.directory / "audit.jsonl").read_text().splitlines()]
    assert len(records) == 2 and all(not row["argv"] for row in records)


def test_timeout_kills_process_group_and_audits(installer, tmp_path, monkeypatch):
    marker = tmp_path / "descendant-survived"
    npm(tmp_path, monkeypatch, f'(sleep .7; touch "{marker}") &\necho waiting\nsleep 30\n')
    monkeypatch.setattr(agent_install, "INSTALL_TIMEOUT", .15)
    code, job = install(installer)
    assert code == 202
    assert install(installer)[0] == 409
    result = finish(installer, job["job"])
    assert result["status"] == "timeout" and result["exit_code"] < 0
    assert "waiting" in result["tail"]
    time.sleep(.75)
    assert not marker.exists()
    record = json.loads((installer.directory / (job["job"] + ".audit.json")).read_text())
    assert record["who"] == "boss" and record["duration_sec"] >= .15
    assert record["status"] == "timeout"


def test_tail_bounded_secrets_absent_fixed_argv(installer, tmp_path, monkeypatch):
    secret = "fixture-secret-do-not-leak-42"
    monkeypatch.setenv("OPENAI_API_KEY", secret)
    npm(tmp_path, monkeypatch,
        'test -z "$OPENAI_API_KEY" || exit 2\n'
        'test "$npm_config_prefix" = "$HOME/.local" || exit 3\n'
        'test "$*" = "install -g @openai/codex" || exit 4\n'
        'head -c 20000 /dev/zero | tr "\\000" x\n'
        f'echo "{secret}"\necho done\n'
        'printf "#!/bin/sh\\nexit 0\\n" > "$(dirname "$0")/codex"\n'
        'chmod +x "$(dirname "$0")/codex"\n')
    code, job = install(installer)
    assert code == 202
    result = finish(installer, job["job"])
    assert result["status"] == "succeeded" and result["exit_code"] == 0
    assert len(result["tail"].encode()) <= 8192
    assert "[REDACTED]" in result["tail"] and "done" in result["tail"]
    for path in installer.directory.glob("*.json*"):
        assert secret not in path.read_text()
    record = json.loads((installer.directory / "audit.jsonl").read_text())
    assert record["argv"] == ["npm", "install", "-g", "@openai/codex"]
    assert set(record) >= {"who", "agent", "argv", "started_at", "exit_code", "duration_sec"}


@pytest.mark.parametrize("script,status", [("exit 7\n", "failed"), ("exec 1>&-\nsleep 30\n", "timeout")])
def test_failure_and_closed_stdout_timeout(installer, tmp_path, monkeypatch, script, status):
    npm(tmp_path, monkeypatch, script)
    monkeypatch.setattr(agent_install, "INSTALL_TIMEOUT", .15)
    _, job = install(installer)
    assert finish(installer, job["job"])["status"] == status


def test_missing_npm(installer, monkeypatch):
    monkeypatch.setenv("PATH", "/missing-bin")
    code, job = install(installer)
    assert code == 400
    assert "npm" in job["error"]


@pytest.mark.parametrize("role", ["operator", "viewer", "admin", None, "machine"])
def test_http_session_boundary_all_routes(http, facts, role):
    server, base = http
    token = None
    if role in ("operator", "viewer", "admin"):
        server.users.add_user("alice", "alice-password-1", role)
        token = server.users.login("alice", "alice-password-1")["token"]
    elif role == "machine":
        token = "test-machine-token-with-adequate-length"
        server.api.add_token("fixture", token)
    expected = 200 if role == "admin" else 403 if role in ("operator", "viewer") else 401
    for path, method, body in [("/api/agents", "GET", None),
                               ("/api/agents/bind", "POST", {}),
                               ("/api/agents/install/" + "a" * 32, "GET", None),
                               ("/api/agents/install", "POST", {"agent": "hermes"})]:
        status, payload = call(base, path, token, method, body)
        if role == "admin":
            assert status == (404 if "/install/" in path else 400 if path.endswith("/install") else 200)
        else:
            assert status == expected


def test_http_install_and_bind(http, facts, tmp_path, monkeypatch):
    server, base = http
    npm(tmp_path, monkeypatch, "echo installed\n")
    codex = tmp_path / "bin" / "codex"
    codex.write_text("#!/bin/sh\nexit 0\n")
    codex.chmod(0o755)
    server.users.add_user("alice", "alice-password-1", "admin")
    token = server.users.login("alice", "alice-password-1")["token"]
    code, job = call(base, "/api/agents/install", token, "POST", {"agent": "codex"})
    assert code == 202
    finish(server.agent_installer, job["job"])
    code, result = call(base, "/api/agents/install/" + job["job"], token)
    assert code == 200 and result["status"] == "succeeded"
    facts["codex"].update(found=True, path="/fixture/codex")
    assert call(base, "/api/agents/install", token, "POST", {"agent": "codex"})[0] == 409
    assert call(base, "/api/agents", token)[1]["preset"][1]["found"]
    assert set(call(base, "/api/agents/bind", token, "POST", {})[1]["bindings"]) == {"zhongshu", "gongbu"}
