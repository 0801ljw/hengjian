"""Offline real SIGKILL/restart drill. Writes evidence only in a new run directory."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import selectors
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from .core import ROOT, Conflict, Store
from .leases import LeaseError, force_release, held_token, read
from .mutate import apply_mutation, payload_hash


def worker(home, mode):
    store = Store(home)
    run = store.checkout("T", True)
    token = held_token(store, run)
    print(json.dumps({"pid": os.getpid(), "run_id": run, "fencing_token": token}), flush=True)
    if mode == "sleep":
        time.sleep(300)
    else:
        store.finish(run, {"status": "succeeded", "artifact": {
            "path": "sqlite:execution_commits/T",
            "sha256": hashlib.sha256(b"single transactional result").hexdigest(),
            "bytes": len(b"single transactional result")}},
            fencing_token=token)


def ready(process):
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ)
        if not selector.select(timeout=10):
            raise RuntimeError("executor did not become ready within 10 seconds")
    line = process.stdout.readline()
    if not line:
        raise RuntimeError(f"executor exited before admission: {process.stderr.read()}")
    return json.loads(line)


def drill(output):
    output.mkdir(parents=True, exist_ok=True)
    home = Path(tempfile.mkdtemp(prefix="kill-restart-", dir=output))
    store = Store(home)
    store.create("T", "Offline SIGKILL recovery")
    for actor, target in (("taizi", "Zhongshu"), ("zhongshu", "Menxia"),
                          ("menxia", "Assigned"), ("shangshu", "Doing")):
        store.move("T", actor, target, "offline drill fixture")
    evidence = {"home": str(home), "events": []}
    def record(kind, **details):
        event = {"kind": kind, **details}
        evidence["events"].append(event)
        print(json.dumps(event), flush=True)

    command = [sys.executable, "-m", "fusion.lease_drill", "--worker", str(home)]
    first = subprocess.Popen(command + ["sleep"], cwd=ROOT, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True)
    try:
        old = ready(first)
        record("first_admitted", **old)
        try:
            force_release(store, "T", old["fencing_token"])
        except LeaseError as exc:
            assert "OWNER_ALIVE" in str(exc)
            record("live_force_release_refused", error=str(exc))
        else:
            raise AssertionError("live executor was force-released")
        os.kill(first.pid, signal.SIGKILL)
        first.communicate(timeout=10)
        assert first.returncode == -signal.SIGKILL
        record("external_SIGKILL", pid=first.pid, returncode=first.returncode)
    finally:
        if first.poll() is None:
            first.kill()
            first.communicate(timeout=10)

    second = subprocess.Popen(command + ["complete"], cwd=ROOT, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True)
    try:
        new = ready(second)
        _, err = second.communicate(timeout=10)
        assert second.returncode == 0, err
    finally:
        if second.poll() is None:
            second.kill()
            second.communicate(timeout=10)
    assert new["fencing_token"] > old["fencing_token"]
    record("restarted_and_completed", **new)
    task = store.get("T")
    attempts = {
        "finish": lambda: store.finish(old["run_id"], {"status": "failed"},
                                        fencing_token=old["fencing_token"]),
        "task_mutation": lambda: apply_mutation(
            store, "T", "gongbu", "payload", "Doing", "simulated resurrection",
            payload=task["payload"], payload_sha256=payload_hash(task["payload"]),
            expected_revision=task["revision"], run_id=old["run_id"],
            fencing_token=old["fencing_token"]),
    }
    before = store.snapshot()
    for kind, attempt in attempts.items():
        try:
            attempt()
        except LeaseError as exc:
            assert "STALE_FENCING_TOKEN" in str(exc)
            record("resurrected_write_rejected", write=kind, error=str(exc))
        else:
            raise AssertionError("stale write succeeded")
    assert store.snapshot() == before
    try:
        store.checkout("T", True)
    except Conflict:
        record("duplicate_dispatch_refused")
    else:
        raise AssertionError("committed task executed again")
    with store.connect() as conn:
        commits = conn.execute("SELECT task_id,run_id,fencing_token,artifact FROM execution_commits").fetchall()
        lease = read(conn, "T")
    snapshot = store.snapshot()
    assert len(commits) == 1
    assert sum(run["status"] == "succeeded" for run in snapshot["runs"]) == 1
    assert sum(event["action"] == "run_finished" for event in snapshot["events"]) == 1
    assert lease["owner_id"] is None
    evidence.update(commits=commits, lease=lease, snapshot=snapshot)
    record("verified", committed_executions=len(commits), successful_runs=1,
           run_finished_events=1, attempts=len(snapshot["runs"]), duplicate_side_effects=0)
    (home / "evidence.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2))
    (home / "output.log").write_text("\n".join(json.dumps(e) for e in evidence["events"]) + "\n")
    return evidence


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--worker", nargs=2, metavar=("HOME", "MODE"))
    args = parser.parse_args()
    if args.worker:
        worker(Path(args.worker[0]), args.worker[1])
    elif args.output:
        drill(args.output.resolve())
    else:
        parser.error("--output required")


if __name__ == "__main__":
    main()
