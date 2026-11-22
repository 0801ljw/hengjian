"""First-run HTTP behavior and explicit admin/bootstrap boundaries, offline."""
import json
from pathlib import Path
import re
import shutil
import subprocess
import urllib.error
import urllib.request

import pytest

from fusion import onboarding
from fusion.role_binding import ROLES
from fusion.tests.test_auth import http, call


@pytest.fixture(autouse=True)
def facts(monkeypatch):
    for key in ("PENGUIN_GPT_API_KEY", "MODELFLARE_CLAUDE_API_KEY", "DEEPSEEK_API_KEY"):
        monkeypatch.setenv(key, "TEST-NOT-A-SECRET")
    value = {name: {"found": name == "codex", "version": "fixture-1.0", "path": "/fixture/" + name}
             for name in ("claude", "codex", "hermes", "pi")}
    monkeypatch.setattr(onboarding, "probe", lambda *a, **kw: value)
    return value


def post(base, endpoint, payload=None, token=None):
    return call(base, "/api/onboarding/" + endpoint, token, "POST", payload or {})


def test_root_single_complete_idempotency_and_private_files(http):
    server, base = http
    home = server.store.home
    assert not (home / "roles.json").exists()
    with urllib.request.urlopen(base + "/") as response:
        assert "初始化向导" in response.read().decode()
    assert call(base, "/api/onboarding")[1]["required"] is True
    code, result = post(base, "single", {"agent": "codex"})
    assert code == 200 and result["mode"] == "single"
    saved = json.loads((home / "roles.json").read_text())
    assert set(saved["bindings"]) == set(ROLES)
    assert {entry["agent"] for entry in saved["bindings"].values()} == {"codex"}
    assert saved["mode"] == "single"
    receipt = json.loads((home / "onboarding.json").read_text())
    assert receipt["completed_at"] and receipt["mode"] == "single"
    assert post(base, "complete") == post(base, "complete") == (200, receipt)
    assert call(base, "/api/onboarding")[1]["required"] is False
    with urllib.request.urlopen(base + "/") as response:
        assert 'id="ob-step1"' not in response.read().decode()
    for name in ("roles.json", "onboarding.json"):
        assert (home / name).stat().st_mode & 0o777 == 0o600


def test_multi_partial_and_completion(http):
    server, base = http
    code, result = post(base, "multi")
    assert code == 200 and result["mode"] == "multi"
    assert set(result["bindings"]) == {"zhongshu", "gongbu"}
    assert set(result["missing"]) == {"taizi", "menxia", "shangshu"}
    saved = json.loads((server.store.home / "roles.json").read_text())
    assert not (set(result["missing"]) & set(saved["bindings"]))
    assert saved["bindings"] == result["bindings"]
    assert post(base, "complete")[1]["mode"] == "multi"


@pytest.mark.parametrize("detected, suggested", [
    (["hermes", "pi"], "hermes"), (["pi"], "pi"),
    (["codex", "claude"], "claude"), ([], None)])
def test_suggested_is_only_first_detected(http, facts, detected, suggested):
    _, base = http
    for name, entry in facts.items():
        entry["found"] = name in detected
    code, result = call(base, "/api/onboarding")
    assert code == 200 and result["suggested"] == suggested
    assert result["capability_order"] == ["claude", "codex", "hermes", "pi"]
    assert isinstance(result["agents"], list)
    assert result["installable"]["hermes"]["reason"] == "需手动安装"
    if detected:
        # Recommendation never prevents choosing a lower-ranked detected agent.
        assert post(base, "single", {"agent": detected[-1]})[0] == 200


@pytest.mark.parametrize("payload", [{"agent": "missing"}, {"agent": []}, {"agent": "codex", "command": "x"}])
def test_single_validation_leaves_no_files(http, payload):
    server, base = http
    assert post(base, "single", payload)[0] == 400
    assert not (server.store.home / "roles.json").exists()
    assert not (server.store.home / "onboarding.json").exists()


def test_bootstrap_does_not_open_business_or_installer(http):
    _, base = http
    assert call(base, "/api/onboarding")[0] == 200
    assert post(base, "multi")[0] == 200
    assert call(base, "/api/tasks")[0] == 401
    assert call(base, "/api/agents")[0] == 401
    assert call(base, "/api/agents/install", method="POST", body={"agent": "codex"})[0] == 401


def test_all_onboarding_endpoints_require_admin_once_users_exist(http):
    server, base = http
    endpoints = [("", "GET", None), ("/single", "POST", {"agent": "codex"}),
                 ("/multi", "POST", {}), ("/complete", "POST", {})]
    machine = "fixture-" + "a" * 48
    server.api.add_token("onboarding-machine", machine)
    server.users.add_user("admin", "fixture-password", "admin")
    server.users.add_user("viewer", "fixture-password", "viewer")
    server.users.add_user("operator", "fixture-password", "operator")
    admin = server.users.login("admin", "fixture-password")["token"]
    viewer = server.users.login("viewer", "fixture-password")["token"]
    operator = server.users.login("operator", "fixture-password")["token"]
    for suffix, method, data in endpoints:
        for denied in (None, viewer, operator, machine):
            assert call(base, "/api/onboarding" + suffix, denied, method, data)[0] == 403
        assert call(base, "/api/onboarding" + suffix, admin, method, data)[0] == 200


@pytest.mark.parametrize("peer,host,origin,expected", [
    ("127.0.0.1", "127.0.0.1:8787", None, True),
    ("::1", "[::1]:8787", "http://[::1]:8787", True),
    ("10.0.0.2", "localhost:8787", None, False),
    ("127.0.0.1", "evil.example:8787", None, False),
    ("127.0.0.1", "localhost:8787", "http://evil.example", False),
    ("127.0.0.1", "localhost:8787", "null", False)])
def test_anonymous_requires_loopback_and_local_origin(http, peer, host, origin, expected):
    server, _ = http
    assert onboarding.allowed(server.users, None, peer, host, origin) is expected


def test_cross_origin_http_rejected_and_malformed_users_fail_closed(http):
    server, base = http
    req = urllib.request.Request(base + "/api/onboarding", headers={"Origin": "https://evil.example"})
    with pytest.raises(urllib.error.HTTPError) as error:
        urllib.request.urlopen(req)
    assert error.value.code == 403
    server.users.path.write_text('{"users": false}')
    assert call(base, "/api/onboarding")[0] == 503


def test_complete_before_selection_and_malformed_payloads(http):
    server, base = http
    assert post(base, "complete")[0] == 400
    assert post(base, "multi", {"unexpected": True})[0] == 400
    assert onboarding.handle(server.store.home, "POST", "/api/onboarding/single", b"[]")[0] == 400
    assert onboarding.handle(server.store.home, "POST", "/api/onboarding/single", b"{")[0] == 400
    assert call(base, "/api/onboarding/single")[0] == 405


def test_onboarding_css_uses_only_theme_variables():
    web = Path(__file__).resolve().parents[1] / "web"
    css = (web / "style.css").read_text().split("/* 初始化向导：", 1)[1]
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b|rgba?\(|hsla?\(", css)
    assert "var(--" in css
    html = (web / "onboarding.html").read_text()
    assert html.count('class="ob-card"') == 2
    assert "推荐只是推荐" in html


def test_shipped_controller_single_multi_login_install_and_themes():
    node = shutil.which("node")
    if not node:
        pytest.skip("node unavailable")
    result = subprocess.run([node, str(Path(__file__).with_name("onboarding_dom.cjs"))],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_catalog_lists_integrated_and_future_agents(http):
    _, base = http
    code, result = call(base, "/api/onboarding")
    assert code == 200
    rows = {row["id"]: row for row in result["catalog"]}
    assert {"codex", "claude", "pi", "hermes", "gemini", "aider", "cursor-agent",
            "opencode", "openclaw", "qwen", "copilot"} <= set(rows)
    assert rows["codex"]["integrated"] is True
    assert rows["hermes"]["installable"] is False
    assert rows["gemini"]["integrated"] is True
    assert rows["gemini"]["installable"] is True
    assert rows["cursor-agent"]["installable"] is False
    recommendations = {row["role"]: row for row in result["recommendations"]}
    assert set(recommendations) == {"taizi", "zhongshu", "menxia", "shangshu", "gongbu"}
    assert recommendations["menxia"]["agent"] == "claude"
    assert recommendations["menxia"]["model"] == "claude-sonnet-5-5"
