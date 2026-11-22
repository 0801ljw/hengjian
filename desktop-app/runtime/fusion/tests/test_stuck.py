"""Stuck-task audit: read-only by default, only narrow audited blocking."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from fusion.core import PolicyError, Store
from fusion.stuck import apply_block, main, requeue_command, scan
from fusion.tests.board_fixture import keypair


def _store(tmp_path):
    _, pub = keypair(tmp_path / "keys")
    return Store(tmp_path / "db", pub)


def _walk(store, task_id, path):
    store.create(task_id, "stuck fixture")
    for actor, state, *command in path:
        store.move(task_id, actor, state, "fixture",
                   command=command[0] if command else "state")


def _to_doing(store, task_id):
    _walk(store, task_id, [("taizi", "Zhongshu"), ("zhongshu", "Menxia"),
                           ("menxia", "Assigned"), ("shangshu", "Doing")])


def _future(hours=5):
    return datetime.now(timezone.utc) + timedelta(hours=hours)


def test_scan_classifies_gate_and_stalled_stages(tmp_path):
    store = _store(tmp_path)
    _walk(store, "R", [("taizi", "Zhongshu"), ("zhongshu", "Menxia"),
                       ("menxia", "Assigned"), ("shangshu", "Doing"),
                       ("gongbu", "Review", "done")])
    _walk(store, "M", [("taizi", "Zhongshu"), ("zhongshu", "Menxia")])
    _walk(store, "A", [("taizi", "Zhongshu"), ("zhongshu", "Menxia"),
                       ("menxia", "Assigned")])
    _to_doing(store, "D")
    _walk(store, "C", [("taizi", "Cancelled")])

    reports = {report["task_id"]: report for report in scan(store, idle_hours=2,
                                                             now=_future())}
    assert set(reports) == {"R", "M", "A", "D"}          # terminal task excluded
    assert reports["R"]["kind"] == "awaiting_board"
    assert reports["R"]["action"] == "board_sign"
    assert reports["R"]["stuck"] is True
    assert reports["R"]["safe_to_block"] is False
    assert reports["M"]["action"] == "return_to_zhongshu"
    assert reports["M"]["safe_to_block"] is False
    assert reports["A"]["action"] == "block" and reports["A"]["safe_to_block"] is True
    assert reports["D"]["action"] == "block" and reports["D"]["safe_to_block"] is True
    assert all(report["idle_hours"] >= 4 for report in reports.values())


def test_scan_respects_idle_threshold(tmp_path):
    store = _store(tmp_path)
    _walk(store, "M", [("taizi", "Zhongshu"), ("zhongshu", "Menxia")])
    fresh = scan(store, idle_hours=10, now=_future(hours=1))
    assert fresh[0]["stuck"] is False
    old = scan(store, idle_hours=10, now=_future(hours=11))
    assert old[0]["stuck"] is True


def test_apply_block_is_audited_and_narrow(tmp_path):
    store = _store(tmp_path)
    _walk(store, "A", [("taizi", "Zhongshu"), ("zhongshu", "Menxia"),
                       ("menxia", "Assigned")])
    _to_doing(store, "D")
    _walk(store, "M", [("taizi", "Zhongshu"), ("zhongshu", "Menxia")])

    assert apply_block(store, "A", reason="巡检")["state"] == "Blocked"
    assert apply_block(store, "D", reason="巡检")["state"] == "Blocked"

    blocked = {report["task_id"]: report for report in scan(store, now=_future())}
    assert blocked["A"]["kind"] == "blocked" and blocked["A"]["action"] == "needs_board"
    assert blocked["D"]["kind"] == "blocked"

    # Menxia is not blockable, and a second block is refused by the state machine.
    for task_id in ("M", "A"):
        with pytest.raises(PolicyError):
            apply_block(store, task_id, reason="越权")

    snapshot = store.snapshot()
    blocked_events = [event for event in snapshot["events"]
                      if event["task_id"] == "A"
                      and event["detail"].get("to") == "Blocked"]
    assert blocked_events and blocked_events[-1]["detail"]["reason"] == "巡检"


def test_requeue_command_prefers_original_request_and_is_shell_safe(tmp_path):
    task = {"id": "T", "title": "标题", "payload": {"original_request": "读 a.md；写 b.md"}}
    command = requeue_command(task, tmp_path / "instance dir")
    assert command.startswith("fusion run ")
    assert "读 a.md；写 b.md" in command
    assert "--allow-unpriced" in command
    fallback = requeue_command({"id": "T", "title": "只有标题", "payload": {}}, tmp_path)
    assert "只有标题" in fallback


def test_cli_json_and_apply_block(tmp_path, capsys):
    from fusion.tests.board_fixture import keypair as _keypair
    _, pub = _keypair(tmp_path / "keys")
    home = tmp_path / "db"
    store = Store(home, pub)
    _walk(store, "A", [("taizi", "Zhongshu"), ("zhongshu", "Menxia"),
                       ("menxia", "Assigned")])
    assert main(["--home", str(home), "--json", "--hours", "0"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["reports"][0]["kind"] == "stalled_assigned"
    assert main(["--home", str(home), "--apply-block", "--hours", "0",
                 "--run-id", "test:cli"]) == 0
    assert store.get("A")["state"] == "Blocked"
