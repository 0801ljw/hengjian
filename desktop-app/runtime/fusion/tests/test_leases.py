"""Fencing, lease safety, transactional admission and real process recovery."""
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from fusion import leases
from fusion.budget import BudgetError
from fusion.core import ROOT, Conflict, Store
from fusion.lease_drill import drill, ready
from fusion.mutate import apply_mutation, payload_hash
from fusion.tests.test_budget import configured, priced
from fusion.tests.test_fusion import doing


@pytest.fixture
def admitted(tmp_path):
    store = Store(tmp_path)
    doing(store)
    run = store.checkout("T", True)
    yield store, run, leases.held_token(store, run)
    leases.stop_heartbeat(store, run)


def lease(store):
    with store.connect() as conn:
        return leases.read(conn, "T")


def expire(store, run):
    leases.stop_heartbeat(store, run)
    with store.transaction() as conn:
        conn.execute("UPDATE leases SET expires_at=? WHERE task_id='T'", (time.time() - 1,))


def test_acquire_renew_release_and_persistent_token(admitted):
    store, run, token = admitted
    first = lease(store)
    assert first["owner_id"] == run and first["owner_pid"] == os.getpid()
    assert first["fencing_token"] == token == 1
    assert first["expires_at"] > first["heartbeat_at"]
    leases.renew(store, "T", run, token)
    assert lease(store)["heartbeat_at"] >= first["heartbeat_at"]
    assert lease(store)["fencing_token"] == token
    store.finish(run, {"status": "failed"}, fencing_token=token)
    assert lease(store)["owner_id"] is None
    second = store.checkout("T", True)
    assert leases.held_token(store, second) > token
    store.finish(second, {"status": "failed"})


def test_second_owner_rejected_and_live_startup_not_reclaimed(admitted):
    store, run, _ = admitted
    before = store.snapshot()
    other = Store(store.home)
    with pytest.raises(leases.LeaseError, match="LEASE_BUSY"):
        other.checkout("T", True)
    assert store.snapshot() == before
    assert lease(store)["owner_id"] == run


def test_expiry_allows_takeover_and_fences_live_old_owner(admitted):
    store, run, token = admitted
    expire(store, run)
    with pytest.raises(leases.LeaseError, match="EXPIRED|NOT_HELD"):
        store.finish(run, {"status": "failed"}, fencing_token=token)
    other = Store(store.home)
    new = other.checkout("T", True)
    assert leases.held_token(other, new) > token
    assert store.snapshot()["runs"][0]["status"] == "orphaned"
    before = store.snapshot()
    with pytest.raises(leases.LeaseError, match="STALE_FENCING_TOKEN"):
        store.finish(run, {"status": "failed"}, fencing_token=token)
    with pytest.raises(leases.LeaseError, match="STALE_FENCING_TOKEN"):
        leases.renew(store, "T", run, token)
    assert store.snapshot() == before
    other.finish(new, {"status": "failed"})


@pytest.mark.parametrize("command", ["payload", "output", "done", "state", "confirm", "create"])
def test_every_task_writer_rejects_stale_token_before_cas(admitted, command):
    store, run, token = admitted
    expire(store, run)
    replacement = Store(store.home)
    new = replacement.checkout("T", True)
    task = store.get("T")
    before = store.snapshot()
    with pytest.raises(leases.LeaseError, match="STALE_FENCING_TOKEN"):
        apply_mutation(store, "T", "gongbu", command, "Doing", "resurrected write",
                       run_id=run, payload_sha256=payload_hash(task["payload"]),
                       expected_revision=task["revision"], payload=task["payload"],
                       fencing_token=token)
    assert store.snapshot() == before
    replacement.finish(new, {"status": "failed"})


@pytest.mark.parametrize("token", [None, 0, -1, 999, True, "1", 1.0])
def test_missing_future_or_noninteger_fences_rejected(admitted, token):
    store, run, _ = admitted
    task = store.get("T")
    with pytest.raises(leases.LeaseError, match="STALE_FENCING_TOKEN"):
        apply_mutation(store, "T", "gongbu", "payload", "Doing", "invalid fence",
                       run_id=run, payload_sha256=payload_hash(task["payload"]),
                       expected_revision=task["revision"], payload=task["payload"],
                       fencing_token=token)


def test_force_release_refuses_live_pid_even_after_expiry(admitted):
    store, run, token = admitted
    expire(store, run)
    before = lease(store)
    with pytest.raises(leases.LeaseError, match="OWNER_ALIVE"):
        leases.force_release(store, "T", token)
    assert lease(store) == before


def test_dead_pid_force_release_and_single_recovery_event(tmp_path):
    store = Store(tmp_path)
    doing(store)
    child = subprocess.Popen([sys.executable, "-m", "fusion.lease_drill", "--worker",
                              str(tmp_path), "sleep"], cwd=ROOT,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        old = ready(child)
        child.kill()
        child.communicate(timeout=10)
        leases.force_release(store, "T", old["fencing_token"])
        assert lease(store)["owner_id"] is None
        assert lease(store)["fencing_token"] > old["fencing_token"]
        assert store.snapshot()["runs"][0]["status"] == "orphaned"
        Store(tmp_path)
        with store.connect() as conn:
            assert conn.execute("SELECT count(*) FROM budget_events WHERE kind='orphaned'").fetchone()[0] == 1
    finally:
        if child.poll() is None:
            child.kill()
            child.communicate(timeout=10)


def test_budget_failure_rolls_back_lease_run_and_events(configured):
    store, _, _ = configured
    before = store.snapshot()
    with pytest.raises(BudgetError):
        priced(store, estimate=101)
    assert lease(store) is None
    assert store.snapshot() == before
    with store.connect() as conn:
        assert conn.execute("SELECT count(*) FROM budget_reservations").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM budget_events WHERE kind!='policy_changed'").fetchone()[0] == 0


def test_caught_dispatch_error_cannot_commit_partial_lease(configured):
    store, _, _ = configured
    task = store.get("T")
    with store.transaction() as conn:
        with pytest.raises(BudgetError):
            apply_mutation(store, "T", "shangshu", "dispatch", "Doing", "fixture",
                           run_id="rollback", payload_sha256=payload_hash(task["payload"]),
                           expected_revision=task["revision"],
                           payload={"provider": "priced-fixture", "estimated_cents": 101}, conn=conn)
        assert leases.read(conn, "T") is None
        assert conn.execute("SELECT count(*) FROM runs").fetchone()[0] == 0


def test_budget_failure_on_expired_takeover_restores_old_owner(configured):
    store, _, _ = configured
    run = priced(store, estimate=60)
    expire(store, run)
    old = lease(store)
    before = store.snapshot()
    with pytest.raises(BudgetError):
        priced(store, estimate=101)
    assert lease(store) == old
    assert store.snapshot() == before
    assert store.budget_status()["company"]["reserved_cents"] == 60


def test_live_lease_failure_preserves_budget(configured):
    store, _, _ = configured
    run = priced(store, estimate=60)
    before = lease(store)
    with pytest.raises(leases.LeaseError):
        priced(store, estimate=40)
    assert lease(store) == before
    assert store.budget_status()["company"]["reserved_cents"] == 60
    store.finish(run, {"status": "failed", "actual_cents": 0})


def test_concurrent_executors_exactly_one_lease(tmp_path):
    store = Store(tmp_path)
    doing(store)
    barrier = threading.Barrier(2)
    def attempt(_):
        other = Store(tmp_path)
        barrier.wait(timeout=5)
        try:
            return other.checkout("T", True)
        except Conflict:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, range(2)))
    assert sum(result is not None for result in results) == 1
    assert len(store.snapshot()["runs"]) == 1
    assert lease(store)["fencing_token"] == 1
    store.finish(next(result for result in results if result), {"status": "failed"})


def test_background_heartbeat_actually_renews(tmp_path, monkeypatch):
    monkeypatch.setattr(leases, "HEARTBEAT", .02)
    monkeypatch.setattr(leases, "TTL", .2)
    store = Store(tmp_path)
    doing(store)
    run = store.checkout("T", True)
    initial = lease(store)
    deadline = time.monotonic() + 2
    try:
        while lease(store)["heartbeat_at"] <= initial["heartbeat_at"]:
            assert time.monotonic() < deadline, "heartbeat never ran"
            time.sleep(.01)
        assert lease(store)["expires_at"] > initial["expires_at"]
        store.finish(run, {"status": "failed"})
    finally:
        leases.stop_heartbeat(store, run)


def test_revocation_survives_restart_and_committed_effect_cannot_repeat(tmp_path):
    evidence = drill(tmp_path)
    assert len(evidence["commits"]) == 1
    assert evidence["events"][-1]["duplicate_side_effects"] == 0
    assert (next(tmp_path.glob("kill-restart-*")) / "output.log").exists()
    store = Store(next(tmp_path.glob("kill-restart-*")))
    with pytest.raises(Conflict, match="already committed"):
        store.checkout("T", True)


def test_stale_release_rejected_and_no_mutation(admitted):
    store, run, token = admitted
    before = lease(store)
    with store.transaction() as conn:
        with pytest.raises(leases.LeaseError, match="STALE_FENCING_TOKEN"):
            leases.release(conn, "T", run, token - 1)
    assert lease(store) == before


def test_failure_after_budget_reserve_rolls_back_everything(configured, monkeypatch):
    store, _, _ = configured
    before = store.snapshot()
    def fail_event(*args):
        raise RuntimeError("injected post-reservation failure")
    monkeypatch.setattr(store, "event", fail_event)
    with pytest.raises(RuntimeError, match="post-reservation"):
        priced(store)
    assert lease(store) is None
    assert store.snapshot() == before
    with store.connect() as conn:
        assert conn.execute("SELECT count(*) FROM budget_reservations").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM budget_events WHERE kind!='policy_changed'").fetchone()[0] == 0


def test_two_real_processes_only_one_admitted(tmp_path):
    store = Store(tmp_path)
    doing(store)
    code = """
import json, sys
from pathlib import Path
from fusion.core import Store, Conflict
s = Store(Path(sys.argv[1]))
sys.stdin.readline()
try:
    run = s.checkout('T', True)
except Conflict:
    print(json.dumps({'outcome': 'denied'}), flush=True)
    sys.stdin.readline()
else:
    print(json.dumps({'outcome': 'admitted', 'run_id': run}), flush=True)
    sys.stdin.readline()
    s.finish(run, {'status': 'failed'})
"""
    children = []
    try:
        for _ in range(2):
            children.append(subprocess.Popen([sys.executable, "-c", code, str(tmp_path)],
                                             cwd=ROOT, stdin=subprocess.PIPE,
                                             stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                             text=True))
        for child in children:
            child.stdin.write("go\n")
            child.stdin.flush()
        results = [ready(child) for child in children]
        assert sorted(result["outcome"] for result in results) == ["admitted", "denied"]
        assert len(store.snapshot()["runs"]) == 1
        assert lease(store)["owner_id"] == next(r["run_id"] for r in results if "run_id" in r)
        for child in children:
            child.stdin.write("finish\n")
            child.stdin.flush()
            _, err = child.communicate(timeout=10)
            assert child.returncode == 0, err
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
                child.communicate(timeout=10)


def test_lease_status_cli_exposes_committed_execution(tmp_path):
    import json
    evidence = drill(tmp_path)
    result = subprocess.run([sys.executable, "-m", "fusion.control", "--home",
                             evidence["home"], "lease-status"], cwd=ROOT,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    status = json.loads(result.stdout)
    assert status["leases"][0]["owner_id"] is None
    assert len(status["execution_commits"]) == 1


def test_direct_settlement_rejects_stale_fence(admitted):
    from fusion.budget import settle
    store, run, token = admitted
    with store.transaction() as conn:
        with pytest.raises(leases.LeaseError, match="STALE_FENCING_TOKEN"):
            settle(conn, run, {"status": "failed"}, fencing_token=token - 1)
    assert store.snapshot()["runs"][0]["status"] == "running"


def test_committed_output_cannot_be_replaced_even_by_current_token(admitted):
    store, run, token = admitted
    store.finish(run, {"status": "succeeded", "artifact": {"path": "fixture"}})
    before = store.snapshot()
    with pytest.raises(Conflict, match="already committed"):
        store.move("T", "gongbu", "Doing", "repeat output", command="output",
                   payload={"output": {"path": "duplicate"}}, fencing_token=token)
    assert store.snapshot() == before
