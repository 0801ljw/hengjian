import json
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import pytest

from fusion.core import ROOT, Conflict, PolicyError, Store, authorize, check_edge, check_transition, kb
from fusion.dashboard import make_server
from fusion.runtime import CodexLocal, RunRequest


def approved_finish_request(store, tmp_path):
    from fusion.approvals import prepare_request, approval_document, import_approval
    from fusion.tests.board_fixture import keypair, envelope
    from fusion.mutate import payload_hash
    from datetime import datetime, timedelta, timezone
    private, public = keypair(tmp_path / "board-test-keys")
    store.board_key_file = public
    task = store.get("T")
    request = prepare_request(store, "T", "shangshu", "Done", "review done",
                              run_id="local:shangshu", expected_revision=task["revision"],
                              payload_sha256=payload_hash(task["payload"]))
    doc = approval_document(request, actor="board:fixture", run_id="human-fixture",
                            expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat())
    import_approval(store, envelope(doc, private))
    return doc["id"]


def doing(store):
    store.create("T", "合成内部任务")
    for actor, state, recipient in [("taizi", "Zhongshu", "zhongshu"),
                                     ("zhongshu", "Menxia", "menxia"),
                                     ("menxia", "Assigned", "shangshu"),
                                     ("shangshu", "Doing", "gongbu")]:
        store.move("T", actor, state, "test fixture", recipient=recipient)


def test_every_canonical_transition_and_unknown_closed():
    states = set(kb._VALID_TRANSITIONS) | {"Done", "Cancelled", "Unknown"}
    for old in states:
        for target in states:
            if target in kb._VALID_TRANSITIONS.get(old, set()):
                check_transition(old, target)
            else:
                with pytest.raises(PolicyError):
                    check_transition(old, target)


def test_every_original_command_permission_and_unknown_closed():
    commands = set().union(*(p["commands"] for p in kb.AGENT_POLICY.values())) | {"unknown"}
    for actor in list(kb.AGENT_POLICY) + ["unknown", ""]:
        for command in commands:
            if command in kb.AGENT_POLICY.get(actor, {}).get("commands", set()):
                authorize(actor, command)
            else:
                with pytest.raises(PolicyError):
                    authorize(actor, command)


def test_every_original_communication_edge():
    agents = json.loads((ROOT / "agents.json").read_text())
    for actor in agents:
        for target in agents:
            if target["id"] in actor["subagents"]["allowAgents"]:
                check_edge(actor["id"], target["id"])
            else:
                with pytest.raises(PolicyError):
                    check_edge(actor["id"], target["id"])
    with pytest.raises(PolicyError):
        check_edge("unknown", "shangshu")


def test_review_confirmation_owner_and_terminal(tmp_path):
    store = Store(tmp_path)
    doing(store)
    with pytest.raises(PolicyError):
        store.move("T", "gongbu", "Done", "bypass", command="done")
    with pytest.raises(PolicyError):
        store.move("T", "gongbu", "Review", "bypass", command="state")
    store.move("T", "gongbu", "Review", "execution done", command="done", recipient="shangshu")
    approval_id = approved_finish_request(store, tmp_path)
    store.move("T", "shangshu", "Done", "review done", approval_id=approval_id)
    assert store.get("T")["state"] == "PendingConfirm"
    with pytest.raises(PolicyError):
        store.move("T", "shangshu", "Done", "self approve", command="confirm")
    store.move("T", "menxia", "Done", "independent confirmation", command="confirm")
    with pytest.raises(PolicyError):
        store.move("T", "menxia", "Doing", "resurrect")
    assert Store(tmp_path).get("T")["state"] == "Done"


def test_direct_pending_confirm_rejected_without_mutation(tmp_path):
    store = Store(tmp_path)
    doing(store)
    store.move("T", "gongbu", "Review", "execution done", command="done")
    before = store.snapshot()
    with pytest.raises(PolicyError, match="PendingConfirm must be entered"):
        store.move("T", "shangshu", "PendingConfirm", "direct state request", command="state")
    assert store.snapshot() == before
    assert "pending_confirm" not in store.get("T")
    # The rejected request leaves the legitimate confirmation path usable.
    approval_id = approved_finish_request(store, tmp_path)
    store.move("T", "shangshu", "Done", "review done", approval_id=approval_id)
    pending = store.get("T")["pending_confirm"]
    assert pending["target_state"] == "Done" and pending["confirm_by"] == "menxia"
    assert pending["approval_id"] == approval_id
    store.move("T", "menxia", "Done", "confirm", command="confirm")
    assert store.get("T")["state"] == "Done"
    assert "pending_confirm" not in store.get("T")


def test_menxia_can_reject_and_wrong_owner_cannot_approve(tmp_path):
    store = Store(tmp_path)
    store.create("T", "合成任务")
    store.move("T", "taizi", "Zhongshu", "route")
    store.move("T", "zhongshu", "Menxia", "plan")
    with pytest.raises(PolicyError):
        store.move("T", "zhongshu", "Assigned", "self approve")
    store.move("T", "menxia", "Zhongshu", "reject", recipient="zhongshu")
    assert store.get("T")["state"] == "Zhongshu"


def test_unpriced_and_exclusive_checkout_with_concurrent_writers(tmp_path):
    store = Store(tmp_path)
    doing(store)
    with pytest.raises(PolicyError):
        store.checkout("T", False)

    def attempt(_):
        try:
            return Store(tmp_path).checkout("T", True)
        except Conflict:
            return None

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(attempt, range(4)))
    assert sum(x is not None for x in results) == 1
    snapshot = store.snapshot()
    assert len(snapshot["runs"]) == 1
    assert snapshot["runs"][0]["cost_usd"] is None
    run_id = next(x for x in results if x)
    store.finish(run_id, {"status": "failed", "error": "fixture failure"})
    assert store.get("T")["state"] == "Doing", "failed execution cannot advance task"
    with pytest.raises(Conflict):
        store.finish(run_id, {"status": "succeeded"})


def test_no_incomplete_todo_completion(tmp_path):
    store = Store(tmp_path)
    doing(store)
    with store.transaction() as conn:
        task = store.get("T", conn)
        task["todos"] = [{"id": "1", "status": "in-progress"}]
        conn.execute("UPDATE tasks SET body=? WHERE id='T'", (json.dumps(task),))
    with pytest.raises(PolicyError):
        store.move("T", "gongbu", "Review", "unfinished", command="done")


def test_runtime_missing_and_timeout_keep_logs(tmp_path, monkeypatch):
    # Fault injection never calls a model or substitutes for the real E2E evidence.
    import fusion.runtime as runtime

    original = subprocess.Popen
    def missing(*args, **kwargs):
        raise FileNotFoundError("fixture missing CLI")
    monkeypatch.setattr(runtime.subprocess, "Popen", missing)
    request = RunRequest("R", "T", "gongbu", tmp_path, "fixture", 1)
    assert CodexLocal().execute(request).status == "unavailable"
    def hanging(command, **kwargs):
        return original([sys.executable, "-c", "import time; time.sleep(30)"], **kwargs)
    monkeypatch.setattr(runtime.subprocess, "Popen", hanging)
    result = CodexLocal().execute(request)
    assert result.status == "timeout"
    assert result.exit_code is not None
    assert (tmp_path / "runtime.jsonl").exists()


def test_fusion_dashboard_without_openclaw_and_post_denied(tmp_path):
    store = Store(tmp_path)
    store.create("T", "本地无 OpenClaw 测试")
    server = make_server(store, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        for path in ["/", "/api/live-status", "/api/fusion/status"]:
            with urllib.request.urlopen(base + path, timeout=3) as response:
                assert response.status == 200
                if path == "/api/live-status":
                    assert json.load(response)["tasks"][0]["id"] == "T"
        with pytest.raises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(urllib.request.Request(base + "/api/advance-state", data=b"{}"), timeout=3)
        assert caught.value.code == 405
        for path in ("/api/morning-config", "/api/agents-status", "/api/task-output/T"):
            with pytest.raises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(base + path, timeout=3)
            assert caught.value.code == 404
        assert not (tmp_path / "morning_brief_config.json").exists()
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_original_dashboard_starts_without_openclaw(tmp_path):
    import socket
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    with (tmp_path / "upstream-dashboard.log").open("w") as log:
        proc = subprocess.Popen([sys.executable, str(ROOT / "dashboard/server.py"), "--port", str(port)],
                                cwd=ROOT, stdout=log, stderr=log)
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                assert proc.poll() is None, (tmp_path / "upstream-dashboard.log").read_text()
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=.2) as response:
                        assert response.status == 200
                        break
                except (OSError, urllib.error.URLError):
                    time.sleep(.05)
            else:
                pytest.fail("upstream dashboard did not start")
        finally:
            proc.terminate()
            proc.wait(timeout=5)
