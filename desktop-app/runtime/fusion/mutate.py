"""The only authoritative task writer. No HTTP or model calls."""
from __future__ import annotations

import hashlib
import json

from .core import (Conflict, PolicyError, authorize, authorize_effective, check_edge,
                    check_transition, kb, now)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False)


def payload_hash(payload):
    return hashlib.sha256(canonical(payload).encode()).hexdigest()


def scope(old, target, command):
    return {"from": old, "to": target, "command": command}


def apply_mutation(store, task_id, actor, command, target, reason, *, payload_sha256,
                   expected_revision, run_id, payload=None, recipient=None,
                   approval_id=None, fencing_token=0, conn=None):
    """CAS + policy + approval consumption + event in one SQLite transaction.

    conn is reserved for the controller's run-finalization transaction. Callers
    cannot opt out of any policy check by supplying it.
    """
    if conn is None:
        with store.transaction() as connection:
            result = apply_mutation(store, task_id, actor, command, target, reason,
                                    payload_sha256=payload_sha256, expected_revision=expected_revision,
                                    run_id=run_id, payload=payload, recipient=recipient,
                                    approval_id=approval_id, fencing_token=fencing_token, conn=connection)
        if command == "dispatch":
            from .leases import remember
            remember(store, task_id, run_id, result["fencing_token"])
        store.project()
        return result
    if not all(isinstance(x, str) and x for x in (actor, reason, run_id, payload_sha256)):
        raise PolicyError("actor, reason, run_id and payload hash are required")
    if type(expected_revision) is not int or expected_revision < 0:
        raise PolicyError("expected_revision must be a nonnegative integer")
    if command == "dispatch":
        conn.execute("SAVEPOINT budget_dispatch")
        try:
            result = _dispatch(store, conn, task_id, actor, target, payload, run_id,
                               expected_revision, payload_sha256, approval_id, recipient)
        except BaseException:
            conn.execute("ROLLBACK TO budget_dispatch")
            conn.execute("RELEASE budget_dispatch")
            raise
        conn.execute("RELEASE budget_dispatch")
        return result
    from .leases import check
    check(conn, task_id, fencing_token)
    fingerprint = payload_hash({"task_id": task_id, "actor": actor, "command": command,
                                "target": target, "reason": reason, "payload_sha256": payload_sha256,
                                "revision": expected_revision, "run_id": run_id,
                                "payload": payload, "recipient": recipient,
                                "fencing_token": fencing_token})
    if approval_id:
        previous = conn.execute("SELECT fingerprint,result FROM approval_uses WHERE approval_id=?",
                                (approval_id,)).fetchone()
        if previous:
            if previous[0] != fingerprint:
                raise Conflict("409: approval already used for another request")
            return json.loads(previous[1])
    if command == "create":
        if approval_id:
            raise PolicyError("approval_id is only valid for high-risk transitions")
        authorize(actor, command)
        if actor != "taizi" or target != "Taizi" or expected_revision != 0:
            raise PolicyError("create requires taizi / Taizi / revision 0")
        if conn.execute("SELECT 1 FROM tasks WHERE id=?", (task_id,)).fetchone():
            raise Conflict("409: task exists")
        if not isinstance(payload, dict) or payload_hash(payload) != payload_sha256:
            raise PolicyError("payload hash mismatch")
        task = {"id": task_id, "title": payload["title"], "state": "Taizi", "org": "太子",
                "eta": "-", "block": "无", "output": "", "official": "太子",
                "flow_log": [], "todos": [], "revision": 0, "payload": payload}
        old = None
    else:
        task = store.get(task_id, conn)
        if task.get("revision", 0) != expected_revision:
            raise Conflict("409: stale task revision")
        if payload_hash(task.get("payload", {})) != payload_sha256:
            raise PolicyError("payload hash mismatch")
        old = task["state"]
        if approval_id and (old, target) not in kb.HIGH_RISK_TRANSITIONS:
            raise PolicyError("approval_id is only valid for high-risk transitions")
        if command in {"payload", "output"}:
            authorize(actor, "progress")
            owner = _owner(old)
            if actor != owner or target != old or not isinstance(payload, dict):
                raise PolicyError("payload update requires state owner and unchanged state")
            if command == "output" and (actor != "gongbu" or old != "Doing"):
                raise PolicyError("output requires Doing / gongbu")
            if command == "output" and conn.execute(
                    "SELECT 1 FROM execution_commits WHERE task_id=?", (task_id,)).fetchone():
                raise Conflict("task result already committed; duplicate output refused")
            canonical(payload)
            task["payload"] = payload
            if command == "output":
                task["output"] = payload["output"]["path"]
        else:
            if payload is not None or command not in {"state", "done", "confirm", "cancel", "block"}:
                raise PolicyError("invalid state mutation command or payload")
            if command == "cancel":
                from . import policy as _policy
                _policy.check_cancel(actor, old, target)
                authorize_effective(actor, command)
            elif command == "block":
                from . import policy as _policy
                _policy.check_block(actor, old, target)
                authorize(actor, command)
            elif command == "confirm":
                authorize_effective(actor, command)
            else:
                authorize(actor, command)
            check_transition(old, target)
            if target == "PendingConfirm":
                raise PolicyError("PendingConfirm must be entered through a high-risk transition")
            if recipient:
                check_edge(actor, recipient)
            if target in {"Review", "Done"} and any(
                    t.get("status") != "completed" for t in task.get("todos", [])):
                raise PolicyError("incomplete todos prevent completion")
            if old == "PendingConfirm" and command == "state" and target == "Review":
                # Audited escape hatch: expiry or a stale approval must not
                # leave the task permanently stuck. The state owner reopens
                # the case for a fresh review; pending approval is discarded.
                if actor != _owner(old):
                    raise PolicyError(f"state owner is {_owner(old)}, not {actor}")
                task.pop("pending_confirm", None)
            elif old == "PendingConfirm":
                pending = task.get("pending_confirm", {})
                if command != "confirm" or actor != pending.get("confirm_by"):
                    raise PolicyError("wrong confirmation authority")
                if target != pending.get("target_state") or not pending.get("approval_id"):
                    raise PolicyError("confirmation target changed or missing approval")
                from .approvals import validate_approval, independent, insert_review
                decision = validate_approval(store, conn, pending["approval_id"], task,
                                             pending["scope"], pending["request_actor"],
                                             pending["request_run_id"], pending["request_revision"])
                if expected_revision != decision["revision"] + 1:
                    raise PolicyError("approval stale after pending task change")
                independent(actor, run_id, decision["request_actor"], decision["request_run_id"])
                if actor == "menxia":
                    insert_review(store, conn, task, actor, run_id, pending["scope"],
                                  decision["request_actor"], decision["request_run_id"], reason)
                task.pop("pending_confirm")
            else:
                if actor != _owner(old):
                    raise PolicyError(f"state owner is {_owner(old)}, not {actor}")
                if command == "confirm":
                    raise PolicyError("confirm requires PendingConfirm")
                if command == "done" and (old != "Doing" or target != "Review"):
                    raise PolicyError("execution can only report Review")
                if old == "Doing" and command not in {"done", "cancel", "block"}:
                    raise PolicyError("execution must report through done")
                if actor == "menxia":
                    from .approvals import insert_review
                    insert_review(store, conn, task, actor, run_id, scope(old, target, command),
                                  task.get("last_actor", "legacy:unknown"),
                                  task.get("last_run_id", "legacy:unknown"), reason)
                if (old, target) in kb.HIGH_RISK_TRANSITIONS:
                    from .approvals import validate_approval
                    bound_scope = scope(old, target, command)
                    validate_approval(store, conn, approval_id, task, bound_scope,
                                      actor, run_id, expected_revision, reason=reason, recipient=recipient)
                    task["pending_confirm"] = {
                        "target_state": target, "confirm_by": kb.CONFIRM_AUTHORITY[old],
                        "approval_id": approval_id, "scope": bound_scope,
                        "request_actor": actor, "request_run_id": run_id,
                        "request_revision": expected_revision}
                    target = "PendingConfirm"
    task.update(state=target, org=kb.STATE_ORG_MAP.get(target, task.get("org", "")),
                now=reason, updatedAt=now(), revision=expected_revision + 1,
                last_actor=actor, last_run_id=run_id, fencing_token=fencing_token)
    if target == "Doing":
        task["org"] = "工部"
    task["flow_log"].append({"at": now(), "from": old, "to": target,
                             "remark": reason, "agent": actor})
    if command == "create":
        conn.execute("INSERT INTO tasks VALUES(?,?)", (task_id, canonical(task)))
    else:
        conn.execute("UPDATE tasks SET body=? WHERE id=?", (canonical(task), task_id))
    store.event(conn, task_id, actor, command, {"from": old, "to": target, "reason": reason,
                                              "payload_sha256": payload_hash(task["payload"]),
                                              "revision": task["revision"], "run_id": run_id,
                                              "fencing_token": fencing_token})
    if approval_id:
        conn.execute("INSERT INTO approval_uses VALUES(?,?,?)", (approval_id, fingerprint, canonical(task)))
    return task


def _owner(state):
    return {"Assigned": "shangshu", "Next": "shangshu", "Doing": "gongbu"}.get(
        state, kb._STATE_AGENT_MAP.get(state))


def _dispatch(store, conn, task_id, actor, target, request, run_id,
              revision, sha256, approval_id, recipient):
    """The sole dispatch writer: policy + CAS + budget + run insert atomically."""
    authorize(actor, "state")
    if actor != "shangshu" or target != "Doing" or approval_id or recipient:
        raise PolicyError("dispatch requires shangshu / Doing; approval cannot bypass budget")
    if not isinstance(request, dict) or set(request) - {
            "provider", "estimated_cents", "allow_unpriced", "budget_override"}:
        raise PolicyError("invalid dispatch quote")
    task = store.get(task_id, conn)
    if task["revision"] != revision:
        raise Conflict("409: stale task revision")
    if payload_hash(task["payload"]) != sha256:
        raise PolicyError("payload hash mismatch")
    if task["state"] != "Doing":
        raise PolicyError("runtime admission requires Doing")
    if conn.execute("SELECT 1 FROM runs WHERE id=?", (run_id,)).fetchone():
        raise Conflict("409: task or run already has an execution")
    if conn.execute("SELECT 1 FROM execution_commits WHERE task_id=?", (task_id,)).fetchone():
        raise Conflict("task result already committed")
    from .leases import acquire
    token = acquire(conn, task_id, run_id)
    if conn.execute("SELECT 1 FROM runs WHERE task_id=? AND status='running'", (task_id,)).fetchone():
        raise Conflict("409: task already has an execution")
    task["fencing_token"] = token
    conn.execute("UPDATE tasks SET body=? WHERE id=?", (canonical(task), task_id))
    # FK run row is inserted in this same transaction before its reservation.
    body = {"id": run_id, "task_id": task_id, "started_at": now(),
            "agent": "gongbu", "cost_usd": None, "fencing_token": token}
    conn.execute("INSERT INTO runs VALUES(?,?,?,?)", (run_id, task_id, "running", canonical(body)))
    from .budget import reserve
    body.update(reserve(store, conn, run_id, task_id, "gongbu", request, fencing_token=token))
    conn.execute("UPDATE runs SET body=? WHERE id=?", (canonical(body), run_id))
    store.event(conn, task_id, actor, "dispatch", body)
    return body
