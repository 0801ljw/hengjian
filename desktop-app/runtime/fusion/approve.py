"""Apply a board-signed approval to the request it was signed for.

`fusion run` stops at `awaiting_board` and writes board-request.json. The human
signs that document off-host (fusion/board-sign.py) and brings back an envelope.
This command closes the loop without the operator having to guess the binding
fields: it takes them from the frozen request, so the audit chain cannot drift.

  fusion approve --request <board-request.json> --envelope <signed-envelope.json> \
      --home <instance> --board-key-file <public.pem>
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .approvals import import_approval
from .core import Conflict, PolicyError, Store
from .mutate import apply_mutation, payload_hash

# who is allowed to answer the confirmation prompt produced by each high-risk move
CONFIRMERS = {"Review": "menxia", "Doing": "shangshu", "Menxia": "zhongshu"}


def apply_signed(store: Store, request: dict, envelope: dict, *,
                 board_key: Path | None = None) -> dict:
    """Import the signature and submit the exact mutation it authorises."""
    for field in ("task_id", "actor", "run_id", "revision", "payload_sha256", "scope"):
        if field not in request:
            raise PolicyError(f"request is missing {field}")
    task = store.get(request["task_id"])
    if task.get("revision") != request["revision"]:
        raise Conflict("409: request is stale (task revision moved)")
    if payload_hash(task.get("payload", {})) != request["payload_sha256"]:
        raise PolicyError("request no longer matches the task payload")

    approval = import_approval(store, envelope)

    target = request["scope"]["to"]
    result = apply_mutation(
        store, request["task_id"], request["actor"], request["scope"]["command"], target,
        request["reason"], payload_sha256=request["payload_sha256"],
        run_id=request["run_id"], expected_revision=request["revision"],
        approval_id=approval["id"], recipient=request.get("recipient"),
        fencing_token=task.get("fencing_token", 0))
    return {"approval": approval, "task": result or store.get(request["task_id"])}


def confirm(store: Store, task_id: str, *, reason: str, run_id: str) -> dict:
    """Answer the pending confirmation as the designated second party."""
    task = store.get(task_id)
    pending = task.get("pending_confirm") or {}
    expected = pending.get("confirm_by")
    if not expected:
        raise PolicyError("task has no pending confirmation")
    target = pending["target_state"]
    apply_mutation(store, task_id, expected, "confirm", target, reason,
                   payload_sha256=payload_hash(task["payload"]), run_id=run_id,
                   expected_revision=task["revision"],
                   fencing_token=task.get("fencing_token", 0))
    return store.get(task_id)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="fusion approve", description=__doc__.splitlines()[0])
    parser.add_argument("--request", type=Path, required=True, help="frozen board-request.json")
    parser.add_argument("--envelope", type=Path, required=True, help="signed envelope from board-sign.py")
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--board-key-file", type=Path, required=True)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    store = Store(args.home, args.board_key_file)
    request = json.loads(args.request.read_text(encoding="utf-8"))
    envelope = json.loads(args.envelope.read_text(encoding="utf-8"))
    try:
        outcome = apply_signed(store, request, envelope)
        task = outcome["task"]
        state = task.get("state")
        pending = (task.get("pending_confirm") or {}).get("confirm_by")
        final = task
        payload = {"task_id": request["task_id"], "state": state,
                   "pending_confirm_by": pending if state == "PendingConfirm" else None,
                   "approval_id": outcome["approval"]["id"]}
        print(json.dumps(payload, ensure_ascii=False, indent=2) if args.json
              else f"{request['task_id']}: {state}"
                   + (f" (waiting for {pending})" if state == "PendingConfirm" else ""))
        return 0
    except (PolicyError, Conflict, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def confirm_main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="fusion confirm",
                                     description="Answer a pending high-risk confirmation as the designated second party.")
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--board-key-file", type=Path, required=True)
    parser.add_argument("--task", required=True)
    parser.add_argument("--run-id", default=None,
                        help="operator/session identity; defaults to confirm:<pid>:<task>")
    parser.add_argument("--reason", default="second-party confirmation")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    store = Store(args.home, args.board_key_file)
    run_id = args.run_id or f"confirm:{os.getpid()}:{args.task}"
    try:
        task = confirm(store, args.task, reason=args.reason, run_id=run_id)
        payload = {"task_id": args.task, "state": task.get("state"), "run_id": run_id}
        print(json.dumps(payload, ensure_ascii=False, indent=2) if args.json
              else f"{args.task}: {task.get('state')}")
        return 0
    except (PolicyError, Conflict, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
