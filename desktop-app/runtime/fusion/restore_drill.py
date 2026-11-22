"""Destructive recovery drill, restricted to a fresh generated child directory.

No model calls. Ephemeral TEST-ONLY signing key, never the real board key.
Run: python3 -m fusion.restore_drill --output /path/to/new-evidence
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    __package__ = "fusion"

from .approvals import approval_document, import_approval, prepare_request
from .budget import import_policy, ledger, policy
from .core import Store
from .mutate import canonical, payload_hash
from .transfer import read_state


def _signed(document, private):
    path = private.parent / "TEST-ONLY-document.json"
    path.write_text(canonical(document))
    signature = subprocess.run(["openssl", "pkeyutl", "-sign", "-inkey", str(private),
                                "-rawin", "-in", str(path)], check=True, capture_output=True,
                               timeout=10).stdout.hex()
    return {"alg": "ed25519", "document": document, "signature": signature}


def seed(home, public, company):
    private = public.parent / "TEST-ONLY-private.pem"
    subprocess.run(["openssl", "genpkey", "-algorithm", "ED25519", "-out", str(private)],
                   check=True, capture_output=True, timeout=10)
    subprocess.run(["openssl", "pkey", "-in", str(private), "-pubout", "-out", str(public)],
                   check=True, capture_output=True, timeout=10)
    store = Store(home, public, company=company)
    current = datetime.now(timezone.utc)
    expiry = (current + timedelta(hours=1)).isoformat()
    document = {"id": "test-policy", "kind": "budget_policy", "principal": "board",
                "actor": "board:TEST-ONLY", "created_at": current.isoformat(), "expires_at": expiry,
                "revision": 1, "window": "calendar_month_utc", "company_cents": 1000,
                "agents": {"gongbu": 500}, "unpriced": "block"}
    import_policy(store, _signed(document, private))
    for task_id in ("T", "PENDING", "OPEN"):
        store.create(task_id, "Synthetic recovery fixture: " + task_id)
        if task_id == "OPEN":
            continue
        for actor, target in (("taizi", "Zhongshu"), ("zhongshu", "Menxia"),
                              ("menxia", "Assigned"), ("shangshu", "Doing")):
            store.move(task_id, actor, target, "drill fixture")
        if task_id == "T":
            run = store.checkout(task_id, provider="offline-meter-fixture", estimated_cents=90)
            artifact = home.parent / "external-artifact.txt"
            artifact.write_text("synthetic local artifact; intentionally outside backup")
            store.finish(run, {"status": "succeeded", "actual_cents": 30,
                               "artifact": {"path": str(artifact), "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest()}})
        store.move(task_id, "gongbu", "Review", "drill review", command="done")
        task = store.get(task_id)
        request = prepare_request(store, task_id, "shangshu", "Done", "drill board review",
                                  run_id="local:shangshu", expected_revision=task["revision"],
                                  payload_sha256=payload_hash(task["payload"]))
        doc = approval_document(request, actor="board:TEST-ONLY", run_id="test-human-session", expires_at=expiry)
        import_approval(store, _signed(doc, private))
        store.move(task_id, "shangshu", "Done", "drill board review", approval_id=doc["id"])
        if task_id == "T":
            store.move(task_id, "menxia", "Done", "drill independent confirm", command="confirm")
    private.unlink()  # restore can only verify, never sign
    return inspect(home, public, company)


def inspect(home, public, company):
    store = Store(home, public, company=company)
    with store.connect() as conn:
        state = read_state(conn)
        config = policy(conn)
    status = store.budget_status()
    used = status["company"]["known_spend_cents"] + status["company"]["reserved_cents"]
    return {"record_counts": {t: len(v["rows"]) for t, v in state.items()},
            "state_sha256": hashlib.sha256(canonical(state).encode()).hexdigest(),
            "snapshot": store.snapshot(), "ledger": ledger(store), "budget": status,
            "company_remaining_cents": config["company_cents"] - used,
            "agent_remaining_cents": config["agents"]["gongbu"] - used}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--company", default="drill")
    parser.add_argument("--worker", choices=("seed", "inspect"))
    parser.add_argument("--home", type=Path)
    parser.add_argument("--public", type=Path)
    args = parser.parse_args()
    if args.worker:
        print(canonical((seed if args.worker == "seed" else inspect)(args.home, args.public, args.company)))
        return 0
    if args.output is None:
        parser.error("--output is required and must not already exist")
    output = args.output.absolute()
    output.mkdir(parents=True, exist_ok=False)
    original, restored = output / "original-store", output / "restored-store"
    public, bundle = output / "TEST-ONLY-public.pem", output / "company.tar.gz"
    result = {"status": "failed", "company": args.company}
    log = output / "drill.log"

    def run(module, *arguments):
        command = [sys.executable, "-m", module, *map(str, arguments)]
        process = subprocess.run(command, capture_output=True, text=True, timeout=60,
                                 cwd=Path(__file__).resolve().parents[1])
        with log.open("a") as stream:
            stream.write(canonical({"command": command, "returncode": process.returncode,
                                    "stdout": process.stdout, "stderr": process.stderr}) + "\n")
        if process.returncode:
            raise RuntimeError(f"subprocess failed ({process.returncode}): {module}; see drill.log")
        return json.loads(process.stdout)

    try:
        before = run("fusion.restore_drill", "--worker", "seed", "--home", original,
                     "--public", public, "--company", args.company)
        run("fusion", "export", "--home", original, "--out", bundle, "--company", args.company)
        shutil.rmtree(original)
        if original.exists():
            raise RuntimeError("source deletion failed")
        result["source_deleted_before_import"] = True
        with log.open("a") as stream:
            stream.write(canonical({"operation": "shutil.rmtree", "path": str(original), "exists_after": original.exists()}) + "\n")
        run("fusion", "import", "--bundle", bundle, "--home", restored,
            "--company", args.company, "--board-key-file", public)
        after = run("fusion.restore_drill", "--worker", "inspect", "--home", restored,
                    "--public", public, "--company", args.company)
        result.update(before=before, after=after)
        if before != after:
            raise RuntimeError("restored records/state/budget/approval chain differs")
        result.update(status="passed", checks={key: True for key in before})
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    (output / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(canonical({"status": result["status"], "result": str(output / "result.json"), "log": str(log)}))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
