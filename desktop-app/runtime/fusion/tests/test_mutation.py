"""P1 boundaries: real SQLite, subprocess CLI, HTTP, signatures and concurrent CAS."""
import copy
import json
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from fusion.approvals import (approval_document, import_approval, insert_review,
                             prepare_request)
from fusion.tests.board_fixture import keypair, signature
from fusion.core import ROOT, Conflict, PolicyError, Store, kb
from fusion.dashboard import make_server
from fusion.mutate import apply_mutation, payload_hash


@pytest.fixture
def store(tmp_path):
    private, public = keypair(tmp_path)
    result = Store(tmp_path / "db", public)
    result.test_private_key = private  # test fixture only; not controller configuration
    return result


def review(store):
    store.create("T", "内部合成任务")
    for actor, target in (("taizi", "Zhongshu"), ("zhongshu", "Menxia"),
                          ("menxia", "Assigned"), ("shangshu", "Doing")):
        store.move("T", actor, target, "fixture")
    store.move("T", "gongbu", "Review", "output verified", command="done")


def approved_request(store, **overrides):
    task = store.get("T")
    args = dict(task_id="T", actor="shangshu", command="state", target="Done",
                reason="review done", expected_revision=task["revision"],
                payload_sha256=payload_hash(task["payload"]), run_id="request-run")
    args.update(overrides)
    request = prepare_request(store, **args)
    document = approval_document(request, actor="board:test-human", run_id="human-session",
                                 expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat())
    envelope = {"alg": "ed25519", "document": document,
                "signature": signature(document, store.test_private_key)}
    import_approval(store, envelope)
    return dict(args, approval_id=document["id"]), envelope


def test_unsigned_agent_cannot_create_approval(store):
    review(store)
    args, envelope = approved_request(store)
    envelope = copy.deepcopy(envelope)
    envelope["document"]["actor"] = "board:spoof"
    with pytest.raises(PolicyError, match="signature"):
        import_approval(store, envelope)
    with pytest.raises(PolicyError, match="only menxia"):
        with store.transaction() as conn:
            insert_review(store, conn, store.get("T", conn), "shangshu", "R", {}, "gongbu", "Q", "reject")
    with pytest.raises(PolicyError, match="valid board"):
        store.move("T", "shangshu", "Done", "no approval")


@pytest.mark.parametrize("same", ["actor", "run"])
def test_ai_self_review_rejected(store, same):
    store.create("T", "fixture")
    store.move("T", "taizi", "Zhongshu", "route")
    store.move("T", "zhongshu", "Menxia", "plan", run_id="submit-run")
    if same == "actor":
        # Menxia revises its own proposal payload: may not review that submission.
        store.move("T", "menxia", "Menxia", "own proposal", command="payload", payload={"amount": 10})
    before = store.snapshot()
    with pytest.raises(PolicyError, match="self review"):
        store.move("T", "menxia", "Assigned", "review", run_id="submit-run" if same == "run" else "another-run")
    assert store.snapshot() == before


def test_same_run_human_approval_rejected(store):
    review(store)
    args, envelope = approved_request(store)
    doc = envelope["document"]
    doc["id"] = "self-run"
    doc["run_id"] = args["run_id"]
    envelope["signature"] = signature(doc, store.test_private_key)
    with pytest.raises(PolicyError, match="self review"):
        import_approval(store, envelope)


def test_agent_cannot_sign_as_board_even_with_valid_fixture_signature(store):
    review(store)
    _, envelope = approved_request(store)
    doc = envelope["document"]
    doc.update(id="agent-forgery", actor="menxia")
    envelope["signature"] = signature(doc, store.test_private_key)
    with pytest.raises(PolicyError, match="board human"):
        import_approval(store, envelope)


@pytest.mark.parametrize("field", ["amount", "plan"])
def test_payload_or_amount_change_invalidates_approval(store, field):
    review(store)
    args, _ = approved_request(store)
    store.move("T", "shangshu", "Review", "changed", command="payload", payload={field: "changed"})
    with pytest.raises(Conflict):
        apply_mutation(store, **args)
    args["expected_revision"] = store.get("T")["revision"]
    args["payload_sha256"] = payload_hash(store.get("T")["payload"])
    with pytest.raises(PolicyError, match="binding"):
        apply_mutation(store, **args)
    assert store.get("T")["state"] == "Review"


def test_expiry_checked_on_consumption_and_confirm(store, monkeypatch):
    import fusion.approvals as approvals
    review(store)
    args, envelope = approved_request(store)
    future = datetime.now(timezone.utc) + timedelta(hours=2)
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return future
    monkeypatch.setattr(approvals, "datetime", Clock)
    with pytest.raises(PolicyError, match="expired"):
        apply_mutation(store, **args)
    monkeypatch.setattr(approvals, "datetime", datetime)
    apply_mutation(store, **args)
    monkeypatch.setattr(approvals, "datetime", Clock)
    with pytest.raises(PolicyError, match="expired"):
        store.move("T", "menxia", "Done", "confirm", command="confirm")
    assert store.get("T")["state"] == "PendingConfirm"


def test_scope_initiator_and_reason_are_immutable(store):
    review(store)
    args, _ = approved_request(store)
    for changes in ({"reason": "other"}, {"run_id": "other"}, {"target": "Menxia"}):
        with pytest.raises(PolicyError):
            apply_mutation(store, **dict(args, **changes))
    assert store.get("T")["state"] == "Review"


def test_idempotent_approval_and_independent_confirmation(store):
    review(store)
    args, _ = approved_request(store)
    first = apply_mutation(store, **args)
    before = store.snapshot()
    assert apply_mutation(store, **args) == first
    assert store.snapshot() == before
    with pytest.raises(Conflict):
        apply_mutation(store, **dict(args, reason="reused"))
    with pytest.raises(PolicyError, match="self review"):
        store.move("T", "menxia", "Done", "confirm", command="confirm", run_id=args["run_id"])
    store.move("T", "menxia", "Done", "confirm", command="confirm", run_id="independent-review")
    assert store.get("T")["state"] == "Done"
    before = store.snapshot()
    assert apply_mutation(store, **args) == first
    assert store.snapshot() == before
    assert {d["kind"] for d in before["decisions"]} == {"review_decision", "approval"}
    assert Store(store.home, store.board_key_file).snapshot() == before


def test_pending_metadata_without_approval_is_not_authority(store):
    review(store)
    with store.transaction() as conn:
        task = store.get("T", conn)
        task.update(state="PendingConfirm", pending_confirm={"confirm_by": "menxia", "target_state": "Done"})
        conn.execute("UPDATE tasks SET body=? WHERE id='T'", (json.dumps(task),))
    with pytest.raises(PolicyError, match="missing approval"):
        store.move("T", "menxia", "Done", "forged", command="confirm")


def test_concurrent_task_mutations_exactly_one_cas_winner(store):
    store.create("T", "fixture")
    task = store.get("T")
    barrier = threading.Barrier(2)
    def attempt(_):
        other = Store(store.home)
        barrier.wait(timeout=5)
        try:
            apply_mutation(other, "T", "taizi", "state", "Zhongshu", "route",
                           expected_revision=task["revision"], payload_sha256=payload_hash(task["payload"]), run_id="R")
            return 200
        except Conflict as exc:
            return exc.status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(attempt, range(2))) == [200, 409]
    assert len(store.snapshot()["events"]) == 2


@pytest.mark.parametrize("command,args", [("state", ["Zhongshu"]), ("done", []),
                                            ("confirm", ["approve"]), ("block", ["why"]),
                                            ("create", ["title", "Taizi", "太子", "太子"])])
def test_old_cli_hard_denied_and_direct_functions_cannot_write_projection(store, command, args, monkeypatch):
    store.create("T", "fixture")
    before = store.snapshot()
    result = subprocess.run([sys.executable, str(ROOT / "scripts/kanban_update.py"), command, "T", *args],
                            capture_output=True, text=True)
    assert result.returncode == 2
    assert "LEGACY_MUTATION_DISABLED" in result.stderr
    monkeypatch.setattr(kb, "TASKS_FILE", store.home / "tasks_source.json")
    with pytest.raises(PermissionError):
        kb.cmd_state("T", "Zhongshu")
    assert store.snapshot() == before


def test_direct_json_tamper_never_changes_state_and_dashboard_repairs_it(store):
    store.create("T", "fixture")
    path = store.home / "tasks_source.json"
    path.write_text('[{"id":"T","state":"Done"}]')
    before = store.snapshot()
    assert store.get("T")["state"] == "Taizi"
    server = make_server(store, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        path.write_text('[{"id":"T","state":"Done"}]')
        with urllib.request.urlopen(base + "/api/live-status") as response:
            assert json.load(response)["tasks"][0]["state"] == "Taizi"
        for endpoint in ("advance-state", "review-action", "approval", "auth/setup"):
            with pytest.raises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(urllib.request.Request(base + "/api/" + endpoint, data=b'{}'))
            assert caught.value.code == 405
        assert store.snapshot() == before
        assert json.loads(path.read_text())[0]["state"] == "Taizi"
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def test_original_dashboard_http_also_denies_post(store):
    from http.server import HTTPServer
    from fusion.dashboard import upstream
    store.create("T", "fixture")
    before = store.snapshot()
    server = HTTPServer(("127.0.0.1", 0), upstream.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        for endpoint in ("advance-state", "review-action", "create-task", "approval"):
            with pytest.raises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(urllib.request.Request(
                    f"http://127.0.0.1:{server.server_port}/api/{endpoint}", data=b'{}'))
            assert caught.value.code == 405
        assert store.snapshot() == before
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def test_no_board_key_fails_closed_and_wrong_scope_is_rejected(store):
    review(store)
    args, _ = approved_request(store)
    with pytest.raises(PolicyError, match="not configured"):
        apply_mutation(Store(store.home), **args)
    assert store.get("T")["state"] == "Review"


@pytest.mark.parametrize("created,expires", [("2026-01-01", "2099-01-01"),
                                              ("2099-01-01T00:00:00+00:00", "2099-01-02T00:00:00+00:00"),
                                              ("2020-01-01T00:00:00+00:00", "2020-01-02T00:00:00+00:00")])
def test_invalid_future_or_expired_signed_approval_rejected(store, created, expires):
    review(store)
    _, envelope = approved_request(store)
    doc = envelope["document"]
    doc.update(id="bad-time", created_at=created, expires_at=expires)
    envelope["signature"] = signature(doc, store.test_private_key)
    with pytest.raises(PolicyError):
        import_approval(store, envelope)


def test_pending_payload_edit_invalidates_confirmation_even_when_hash_recomputed(store):
    review(store)
    args, _ = approved_request(store)
    apply_mutation(store, **args)
    store.move("T", "shangshu", "PendingConfirm", "changed amount", command="payload", payload={"amount": 123})
    with pytest.raises(PolicyError, match="stale payload"):
        store.move("T", "menxia", "Done", "confirm", command="confirm")
    assert store.get("T")["state"] == "PendingConfirm"


def test_controller_cli_create_cas_and_board_signing_disabled_in_agent_runtime(tmp_path):
    request = {"task_id": "C", "actor": "taizi", "command": "create", "target": "Taizi",
               "reason": "local fixture", "payload": {"title": "fixture"},
               "payload_sha256": payload_hash({"title": "fixture"}), "expected_revision": 0, "run_id": "C"}
    path = tmp_path / "request.json"
    path.write_text(json.dumps(request))
    cmd = [sys.executable, "-m", "fusion.control", "--home", str(tmp_path / "home"), "mutate", str(path)]
    first = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    assert first.returncode == 0, first.stderr
    assert json.loads(first.stdout)["state"] == "Taizi"
    second = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    assert second.returncode == 3
    assert json.loads(second.stdout)["status"] == 409
    import os
    env = dict(os.environ, PAPERCLIP_RUN_ID="test-agent-runtime")
    sign = subprocess.run([sys.executable, "-m", "fusion.board", "--key-file", "missing",
                           "--request", "missing", "--actor", "board:test", "--run-id", "human",
                           "--expires-at", "2099-01-01T00:00:00+00:00"],
                          cwd=ROOT, env=env, capture_output=True, text=True)
    assert sign.returncode == 2
    assert "disabled inside an agent runtime" in sign.stderr


def test_runtime_output_write_uses_revisioned_mutation_service(store):
    store.create("T", "fixture")
    for actor, target in (("taizi", "Zhongshu"), ("zhongshu", "Menxia"),
                          ("menxia", "Assigned"), ("shangshu", "Doing")):
        store.move("T", actor, target, "fixture")
    before = store.get("T")["revision"]
    run_id = store.checkout("T", True)
    artifact = {"path": "/fixture/result.json", "sha256": "fixture", "bytes": 1}
    store.finish(run_id, {"status": "succeeded", "artifact": artifact})
    assert store.get("T")["revision"] == before + 1
    assert store.get("T")["payload"]["output"] == artifact
    assert store.get("T")["output"] == artifact["path"]
    assert store.snapshot()["events"][-1]["action"] == "output"
