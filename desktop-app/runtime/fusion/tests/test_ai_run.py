"""Offline CLI acceptance: real Pipeline/Store/validation, fake model transport."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

from fusion import ai_run
from fusion.__main__ import main as entry
from fusion.ai import DecisionClient
from fusion.core import Store
from fusion.tests.test_ai import BAD, DISPATCH, GOOD, PLAN, ROUTE, WORK, FakeTransport


@pytest.fixture
def invoke(tmp_path):
    def run(values, *flags, allow=True, json_mode=True):
        transport = FakeTransport(values)
        clients = []

        def factory(directory):
            client = DecisionClient(directory, transport)
            clients.append(client)
            return client

        argv = ["--home", str(tmp_path / "instance"), *flags]
        if allow:
            argv.append("--allow-unpriced")
        if json_mode:
            argv.append("--json")
        code = ai_run.main(argv, client_factory=factory)
        return code, clients, transport
    return run


def test_success_real_pipeline_and_unknown_cost(invoke, capsys, tmp_path):
    code, clients, _ = invoke([ROUTE, PLAN, GOOD, DISPATCH, WORK], "write report.md")
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert code == report["exit_code"] == 0
    assert report["status"] == "awaiting_board" and report["state"] == "Review"
    assert report["total_tokens"] == 75 and report["cost_usd"] is None
    assert report["pricing"] == "unknown" and report["elapsed_sec"] >= 0
    assert (Path(report["workspace"]) / "report.md").read_text() == WORK["files"][0]["content"]
    assert (Path(report["workspace"]) / "board-request.json").is_file()
    for role in ["太子", "中书", "门下", "尚书", "工部"]:
        assert f"[{role}]" in captured.err
    assert "产出" in captured.err and "report.md" in captured.err
    snapshot = Store(tmp_path / "instance").snapshot()
    assert snapshot["tasks"][0]["state"] == "Review"
    assert not any(item["kind"] == "approval" for item in snapshot["decisions"])
    assert clients[0].calls == 5


def test_rejection_feedback_and_live_progress(invoke, capsys, monkeypatch):
    original = FakeTransport.__call__

    def observed(self, request, path):
        # At the next call, prior progress is already visible (not end-of-run replay).
        output = capsys.readouterr()
        if len(self.requests) == 3:
            assert "第1次 reject 理由：unsafe" in output.err
            assert "remove network access" in output.err
        return original(self, request, path)

    monkeypatch.setattr(FakeTransport, "__call__", observed)
    code, clients, transport = invoke(
        [ROUTE, PLAN, BAD, PLAN, GOOD, DISPATCH, WORK], "--request", "write")
    assert code == 0 and clients[0].calls == 7
    assert "remove network access" in transport.requests[3]["messages"][1]["content"]


def test_second_approval_visible(invoke, capsys):
    assert invoke([ROUTE, PLAN, BAD, PLAN, GOOD, DISPATCH, WORK], "write")[0] == 0
    assert "第2次 approve" in capsys.readouterr().err


def test_final_rejection_exit_two(invoke, capsys, tmp_path):
    code, clients, _ = invoke([ROUTE] + [PLAN, BAD] * 3, "write")
    report = json.loads(capsys.readouterr().out)
    assert code == report["exit_code"] == 2
    assert report["status"] == "rejected" and clients[0].calls == 7
    snapshot = Store(tmp_path / "instance").snapshot()
    assert snapshot["tasks"][0]["state"] == "Cancelled" and not snapshot["runs"]


@pytest.mark.parametrize("flags", [[], ["--request", ""], [" "], ["x" * 12001],
                                    ["one", "--request", "two"], ["--bogus"],
                                    ["--company", "../escape", "write"]])
def test_invalid_parameters_no_model(invoke, capsys, flags):
    code, clients, transport = invoke([], *flags)
    report = json.loads(capsys.readouterr().out)
    assert code == report["exit_code"] == 3 and report["status"] == "error"
    assert not clients and not transport.requests


def test_missing_home_json(capsys):
    assert ai_run.main(["write", "--json"]) == 3
    assert "--home" in json.loads(capsys.readouterr().out)["error"]


def test_unpriced_budget_gate_before_worker(invoke, capsys, tmp_path):
    code, clients, _ = invoke([ROUTE, PLAN, GOOD, DISPATCH, WORK], "write", allow=False)
    report = json.loads(capsys.readouterr().out)
    assert code == 3 and "UNPRICED_BLOCKED" in report["error"]
    assert clients[0].calls == 4
    assert not Store(tmp_path / "instance").snapshot()["runs"]


def test_lease_gate_before_worker(invoke, capsys, monkeypatch):
    from fusion import leases

    def busy(*args):
        raise leases.LeaseError("LEASE_BUSY")

    monkeypatch.setattr(leases, "acquire", busy)
    code, clients, _ = invoke([ROUTE, PLAN, GOOD, DISPATCH, WORK], "write")
    assert code == 3 and clients[0].calls == 4
    assert "LEASE_BUSY" in json.loads(capsys.readouterr().out)["error"]


def test_named_company_and_repeated_runs(invoke, capsys, tmp_path):
    ids = []
    for _ in range(2):
        assert invoke([ROUTE, PLAN, GOOD, DISPATCH, WORK], "write", "--company", "alpha")[0] == 0
        report = json.loads(capsys.readouterr().out)
        ids.append(report["task_id"])
        assert "/companies/alpha/" in report["workspace"]
        assert Path(report["audit"]).is_dir()
    assert len(set(ids)) == 2
    assert len(Store(tmp_path / "instance", company="alpha").snapshot()["tasks"]) == 2
    assert not (tmp_path / "instance" / "fusion.sqlite3").exists()


def test_chat_no_task(invoke, capsys, tmp_path):
    code, _, _ = invoke([{**ROUTE, "kind": "chat"}], "hello")
    assert code == 0 and json.loads(capsys.readouterr().out)["status"] == "chat"
    assert not Store(tmp_path / "instance").snapshot()["tasks"]


def test_human_summary_unknown_not_zero(invoke, capsys):
    assert invoke([ROUTE, PLAN, GOOD, DISPATCH, WORK], "write", json_mode=False)[0] == 0
    text = capsys.readouterr().out
    assert "token：75；成本 USD：unknown" in text
    assert "产物路径：" in text and "等待签批处" in text


def test_worker_error_releases_lease(invoke, capsys, tmp_path):
    code, _, _ = invoke([ROUTE, PLAN, GOOD, DISPATCH, OSError("offline")], "write")
    assert code == 3 and json.loads(capsys.readouterr().out)["status"] == "error"
    store = Store(tmp_path / "instance")
    assert store.snapshot()["runs"][0]["status"] == "failed"
    with store.connect() as conn:
        assert conn.execute("SELECT owner_id FROM leases").fetchone() == (None,)


def test_missing_usage_remains_unknown(invoke, capsys, monkeypatch):
    original = FakeTransport.__call__

    def no_usage(self, request, path):
        return {**original(self, request, path), "usage": None}

    monkeypatch.setattr(FakeTransport, "__call__", no_usage)
    assert invoke([ROUTE, PLAN, GOOD, DISPATCH, WORK], "write", json_mode=False)[0] == 0
    assert "token：unknown；成本 USD：unknown" in capsys.readouterr().out


def test_module_dispatch_success(invoke, monkeypatch, tmp_path, capsys):
    factory = lambda directory: DecisionClient(
        directory, FakeTransport([ROUTE, PLAN, GOOD, DISPATCH, WORK]))
    monkeypatch.setattr(ai_run, "DecisionClient", factory)
    assert entry(["run", "write", "--home", str(tmp_path), "--allow-unpriced"]) == 0
    assert "awaiting_board" in capsys.readouterr().out


@pytest.mark.parametrize("command,expected", [
    (["run", "--help"], 0), (["run", "write"], 3),
    (["run", "write", "--bad"], 3), (["--help"], 0)])
def test_module_process_exit_codes(command, expected):
    result = subprocess.run([sys.executable, "-m", "fusion", *command],
                            capture_output=True, text=True, check=False)
    assert result.returncode == expected


def test_old_controller_still_available(tmp_path, capsys):
    assert entry(["snapshot", "--home", str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out)["tasks"] == []
