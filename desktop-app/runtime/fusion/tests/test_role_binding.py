import copy
import gc
import json
from pathlib import Path
import subprocess
import sys

import pytest

from fusion import bind_cli, role_binding
from fusion.__main__ import main
from fusion.ai import DecisionClient, MODEL
from fusion.mutate import payload_hash
from fusion.tests.test_ai import FakeTransport, ROUTE


@pytest.fixture
def agents(monkeypatch):
    for key in ("PENGUIN_GPT_API_KEY", "MODELFLARE_CLAUDE_API_KEY", "DEEPSEEK_API_KEY"):
        monkeypatch.setenv(key, "TEST-NOT-A-SECRET")
    facts = {name: {"found": True, "model": model, "path": "/fixture/" + name,
                    "version": "fixture", "config_exists": True, "env_key": False}
             for name, model in (("codex", MODEL), ("pi", None), ("claude", None))}
    monkeypatch.setattr(bind_cli, "probe", lambda *args, **kwargs: copy.deepcopy(facts))
    return facts


def test_default_backward_compatible(tmp_path):
    value = role_binding.load(tmp_path)
    assert value["mode"] == "single"
    assert set(value["bindings"]) == set(role_binding.ROLES)
    assert all(entry == {"agent": "codex", "model": MODEL} for entry in value["bindings"].values())
    assert not (tmp_path / "roles.json").exists()


@pytest.mark.parametrize("damage", ["missing", "unknown", "empty_model", "extra", "single_mismatch", "bad_entry"])
def test_invalid_binding_rejected(tmp_path, agents, damage):
    binding = role_binding.default()
    if damage == "missing":
        del binding["bindings"]["taizi"]
    elif damage == "unknown":
        binding["bindings"]["taizi"]["agent"] = "uninstalled"
    elif damage == "empty_model":
        binding["bindings"]["taizi"]["model"] = " "
    elif damage == "extra":
        binding["bindings"]["unknown"] = {}
    elif damage == "single_mismatch":
        binding["bindings"]["taizi"]["model"] = "other"
    else:
        binding["bindings"]["taizi"] = None
    with pytest.raises(ValueError):
        role_binding.save(tmp_path, binding, agents)
    assert not (tmp_path / "roles.json").exists()


def test_warnings_independence_and_undetected(agents):
    binding = role_binding.default()
    assert any("审查独立性弱" in item for item in role_binding.warnings(binding, agents))
    assert any("单 agent" in item for item in role_binding.warnings(binding, agents))
    binding["bindings"]["menxia"] = {"agent": "unknown", "model": MODEL}
    warnings = role_binding.warnings(binding, agents)
    assert not any("审查独立性弱" in item for item in warnings)
    assert any("未检测到" in item for item in warnings)


def test_single_cli_write_show_permissions(tmp_path, agents, capsys):
    assert main(["bind", "--home", str(tmp_path), "--mode", "single", "--agent", "codex:test-model"]) == 0
    value = role_binding.load(tmp_path)
    assert value["updated_at"]
    assert all(entry["model"] == "test-model" for entry in value["bindings"].values())
    assert (tmp_path / "roles.json").stat().st_mode & 0o777 == 0o600
    assert "审查独立性弱" in capsys.readouterr().out
    assert main(["bind", "--instance", str(tmp_path), "--show"]) == 0
    assert '"mode": "single"' in capsys.readouterr().out
    (tmp_path / "roles.json").chmod(0o644)
    role_binding.save(tmp_path, value, agents)
    assert (tmp_path / "roles.json").stat().st_mode & 0o777 == 0o600


def test_multi_cli_and_partial_edits(tmp_path, agents):
    args = ["bind", "--home", str(tmp_path), "--mode", "multi"]
    assert main(args + ["--set", "menxia=claude:review"]) != 0
    for role in role_binding.ROLES:
        args += ["--set", role + ("=claude:review" if role == "menxia" else "=codex:plan")]
    assert main(args) == 0
    saved = role_binding.load(tmp_path)
    assert saved["bindings"]["menxia"] == {"agent": "claude", "model": "review"}
    assert main(["bind", "--home", str(tmp_path), "--mode", "multi", "--set", "taizi=pi:fast"]) == 0
    assert role_binding.load(tmp_path)["bindings"]["zhongshu"] == saved["bindings"]["zhongshu"]


@pytest.mark.parametrize("args", [[], ["--mode", "single"], ["--mode", "multi", "--set", "bad=codex:m"],
    ["--mode", "single", "--agent", "unknown:m"], ["--auto", "--agent", "codex:m"],
    ["--show", "--auto"], ["--mode", "single", "--agent", "codex:"]])
def test_bad_cli_never_writes(tmp_path, agents, args):
    assert main(["bind", "--home", str(tmp_path)] + args) != 0
    assert not (tmp_path / "roles.json").exists()


def test_agents_readonly_auto_reasons_and_override(tmp_path, agents, capsys):
    assert main(["agents", "--home", str(tmp_path)]) == 0
    assert not (tmp_path / "roles.json").exists()
    assert "启发式，可用 --set 覆盖" in capsys.readouterr().out
    assert main(["bind", "--home", str(tmp_path), "--auto", "--set", "menxia=pi:custom-review"]) == 0
    output = capsys.readouterr().out
    for role in role_binding.ROLES:
        assert role + ":" in output
    assert "用户 --set 覆盖" in output
    assert role_binding.load(tmp_path)["bindings"]["menxia"] == {"agent": "pi", "model": "custom-review"}


def test_suggestion_independence_and_api_only(agents):
    value, _ = bind_cli.suggest(agents)
    assert value["bindings"]["taizi"]["agent"] == "pi"
    assert value["bindings"]["menxia"]["agent"] == "claude"
    assert value["bindings"]["zhongshu"]["agent"] == "codex"
    only_api = {"api": {"found": True, "model": "configured"}}
    value, _ = bind_cli.suggest(only_api)
    assert value["mode"] == "single"
    assert all(entry == {"agent": "api", "model": "configured"} for entry in value["bindings"].values())
    with pytest.raises(ValueError, match="known agents"):
        role_binding.validate(value, only_api)
    value, _ = bind_cli.suggest({"codex": {"found": False}})
    with pytest.raises(ValueError):
        role_binding.validate(value, {})


def test_decision_model_and_audit_and_fallback(tmp_path):
    transport = FakeTransport([ROUTE, ROUTE])
    client = DecisionClient(tmp_path / "round", transport,
                            bindings={"taizi": {"agent": "pi", "model": "custom"}})
    client.decide("taizi", {})
    assert transport.requests[0]["model"] == "custom"
    record = client.summary()["steps"][0]
    assert record["model"] == "custom"
    assert record["model_sha256"] == payload_hash("custom")
    client.bindings = {}
    client.decide("taizi", {})
    assert transport.requests[1]["model"] == MODEL


def test_explicit_bindings_from_file_and_resume(tmp_path, agents):
    value = role_binding.default()
    for entry in value["bindings"].values():
        entry["model"] = "saved-model"
    role_binding.save(tmp_path, value, agents)
    directory = tmp_path / "ai" / "task"
    transport = FakeTransport([ROUTE, ROUTE])
    bindings = role_binding.load(tmp_path)["bindings"]
    client = DecisionClient(directory, transport, bindings=bindings)
    client.decide("taizi", {})
    assert transport.requests[0]["model"] == "saved-model"
    del client
    gc.collect()
    resumed = DecisionClient.resume(directory, transport, bindings=bindings)
    resumed.decide("taizi", {})
    assert transport.requests[1]["model"] == "saved-model"
    assert resumed.calls == 2


def test_instance_bindings_used_by_named_company_run(tmp_path, agents, capsys):
    from fusion.ai_run import main as run
    from fusion.tests.test_ai import PLAN, GOOD, DISPATCH, WORK

    value = role_binding.default()
    for entry in value["bindings"].values():
        entry["model"] = "instance-model"
    role_binding.save(tmp_path, value, agents)
    transport = FakeTransport([ROUTE, PLAN, GOOD, DISPATCH, WORK])

    def factory(directory, **kwargs):
        return DecisionClient(directory, transport, **kwargs)

    code = run(["write report.md", "--home", str(tmp_path), "--company", "alpha",
                "--allow-unpriced", "--json"], client_factory=factory)
    assert code == 0
    assert json.loads(capsys.readouterr().out)["status"] == "awaiting_board"
    assert len(transport.requests) == 5
    assert all(request["model"] == "instance-model" for request in transport.requests)


@pytest.mark.parametrize("entry", ["module", "launcher"])
def test_process_entrypoints_expose_binding_commands(entry):
    repo = Path(__file__).resolve().parents[2]
    command = [sys.executable, "-m", "fusion"] if entry == "module" else [sys.executable, str(repo / "scripts/fusion")]
    for subcommand in ("agents", "bind"):
        result = subprocess.run(command + [subcommand, "--help"], cwd=repo, capture_output=True,
                                text=True, timeout=5)
        assert result.returncode == 0
        assert "--instance" in result.stdout


def test_auto_with_no_agents_refuses_write(tmp_path, monkeypatch):
    monkeypatch.setattr(bind_cli, "probe", lambda *args, **kwargs: {})
    assert main(["bind", "--auto", "--home", str(tmp_path)]) != 0
    assert not (tmp_path / "roles.json").exists()


def test_multi_same_source_is_rejected():
    binding = role_binding.default()
    binding["mode"] = "multi"
    for role in role_binding.ROLES:
        binding["bindings"][role] = {"agent": "codex", "model": MODEL}
    with pytest.raises(Exception, match="门下必须与中书不同源"):
        role_binding.check_independence(binding)
    binding["bindings"]["menxia"] = {"agent": "claude", "model": "review-model"}
    role_binding.check_independence(binding)


def test_load_rejects_same_source_file(tmp_path):
    binding = role_binding.default()
    binding["mode"] = "multi"
    for role in role_binding.ROLES:
        binding["bindings"][role] = {"agent": "codex", "model": MODEL}
    (tmp_path / "roles.json").write_text(json.dumps(binding))
    with pytest.raises(Exception, match="门下必须与中书不同源"):
        role_binding.load(tmp_path)
