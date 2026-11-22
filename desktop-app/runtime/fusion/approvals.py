"""Offline human approval verification, immutable requests, AI review records."""
from __future__ import annotations

import json
import re
import subprocess
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .core import Conflict, PolicyError, authorize, check_transition, now
from .mutate import _owner, canonical, payload_hash, scope


def timestamp(value):
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            raise ValueError("timezone required")
        return parsed
    except (ValueError, TypeError) as exc:
        raise PolicyError("invalid timezone-aware timestamp") from exc


def live(decision):
    current = datetime.now(timezone.utc)
    if not timestamp(decision["created_at"]) <= current < timestamp(decision["expires_at"]):
        raise PolicyError("decision not yet valid or expired")


def independent(actor, run_id, request_actor, request_run_id):
    if not actor or not run_id or actor == request_actor or run_id == request_run_id:
        raise PolicyError("self review/approval forbidden: same actor or run")


def prepare_request(store, task_id, actor, target, reason, *, run_id,
                    expected_revision, payload_sha256, command="state", recipient=None):
    """Freeze the requester before any board decision can be imported."""
    if command == "cancel":
        if target != "Cancelled":
            raise PolicyError("cancel requests may only target Cancelled")
        from .core import authorize_effective
        authorize_effective(actor, command)
    else:
        authorize(actor, command)
    if command not in {"state", "done", "cancel"} or not run_id or not reason:
        raise PolicyError("invalid request")
    if type(expected_revision) is not int or expected_revision < 0:
        raise PolicyError("expected_revision must be a nonnegative integer")
    with store.transaction() as conn:
        task = store.get(task_id, conn)
        if task.get("revision", 0) != expected_revision:
            raise Conflict("409: stale task revision")
        if payload_hash(task.get("payload", {})) != payload_sha256:
            raise PolicyError("payload hash mismatch")
        if actor != _owner(task["state"]):
            raise PolicyError("requester must own state")
        check_transition(task["state"], target)
        request = {"id": uuid.uuid4().hex, "task_id": task_id, "actor": actor,
                   "run_id": run_id, "revision": expected_revision,
                   "payload_sha256": payload_sha256, "scope": scope(task["state"], target, command),
                   "reason": reason, "recipient": recipient, "created_at": now()}
        conn.execute("INSERT INTO mutation_requests VALUES(?,?,?)",
                     (request["id"], task_id, canonical(request)))
    return request


def approval_document(request, *, actor, run_id, expires_at):
    """Unsigned document for the independent offline board operator to inspect."""
    return {"id": uuid.uuid4().hex, "kind": "approval", "task_id": request["task_id"],
            "request_id": request["id"], "request_actor": request["actor"],
            "request_run_id": request["run_id"], "payload_sha256": request["payload_sha256"],
            "revision": request["revision"], "scope": request["scope"],
            "actor": actor, "run_id": run_id, "created_at": now(),
            "expires_at": expires_at, "decision": "approve", "principal": "board"}


def _verify_signature(store, envelope):
    # No algorithm negotiation or legacy HMAC fallback. The document's canonical
    # bytes are unchanged; alg selects the one supported envelope format.
    if (not isinstance(envelope, dict) or set(envelope) != {"alg", "document", "signature"}
            or envelope["alg"] != "ed25519" or not isinstance(envelope["document"], dict)):
        raise PolicyError("invalid board signature envelope or algorithm")
    signature = envelope["signature"]
    if not isinstance(signature, str) or re.fullmatch(r"[0-9a-f]{128}", signature) is None:
        raise PolicyError("invalid board signature")
    if store.board_key_file is None:
        raise PolicyError("board verification key is not configured")
    try:
        data = canonical(envelope["document"]).encode("utf-8")
        # Some OpenSSL versions accept a private PEM even with -pubin, so enforce
        # the public-only PEM container ourselves before invoking OpenSSL.
        with store.board_key_file.open("rb") as public_file:
            pem = public_file.read(4097)
        if len(pem) > 4096 or re.fullmatch(
                rb"\s*-----BEGIN PUBLIC KEY-----\s+[A-Za-z0-9+/=\r\n]+-----END PUBLIC KEY-----\s*",
                pem) is None:
            raise PolicyError("board verification key must be a public PEM")
        # Also require exact Ed25519 SPKI, not another supported key algorithm.
        key = subprocess.run(["openssl", "pkey", "-pubin", "-outform", "DER"],
                             input=pem, capture_output=True, timeout=10, check=True).stdout
        if len(key) != 44 or key[:12] != bytes.fromhex("302a300506032b6570032100"):
            raise PolicyError("board verification key must be Ed25519 public key")
        with tempfile.TemporaryDirectory(prefix="fusion-board-verify-") as directory:
            root = Path(directory)
            (root / "public.der").write_bytes(key)
            (root / "document").write_bytes(data)
            (root / "signature").write_bytes(bytes.fromhex(signature))
            result = subprocess.run([
                "openssl", "pkeyutl", "-verify", "-pubin", "-keyform", "DER",
                "-inkey", str(root / "public.der"), "-rawin",
                "-in", str(root / "document"), "-sigfile", str(root / "signature")],
                capture_output=True, timeout=10)
        if result.returncode != 0:
            raise PolicyError("invalid board signature")
    except (OSError, subprocess.SubprocessError, ValueError, TypeError) as exc:
        if isinstance(exc, PolicyError):
            raise
        raise PolicyError("invalid board signature or verification key unavailable") from exc


def _request(conn, decision):
    row = conn.execute("SELECT body FROM mutation_requests WHERE id=?",
                       (decision.get("request_id"),)).fetchone()
    if row is None:
        raise PolicyError("missing immutable mutation request")
    request = json.loads(row[0])
    for field, source in (("task_id", "task_id"), ("request_actor", "actor"),
                          ("request_run_id", "run_id"), ("revision", "revision"),
                          ("payload_sha256", "payload_sha256"), ("scope", "scope")):
        if decision.get(field) != request[source]:
            raise PolicyError(f"approval request binding mismatch: {field}")
    return request


def import_approval(store, envelope):
    """Only a signed board assertion can create an approval. No HTTP endpoint."""
    _verify_signature(store, envelope)
    decision = envelope["document"]
    required = {"id", "kind", "task_id", "request_id", "request_actor", "request_run_id",
                "payload_sha256", "revision", "scope", "actor", "run_id", "created_at",
                "expires_at", "decision", "principal"}
    if set(decision) != required or decision["kind"] != "approval" or decision["principal"] != "board":
        raise PolicyError("only board can produce approval")
    if not isinstance(decision["actor"], str) or not decision["actor"].startswith("board:"):
        raise PolicyError("approval actor must be an identified board human")
    if decision["decision"] != "approve":
        raise PolicyError("not an affirmative approval")
    independent(decision["actor"], decision["run_id"], decision["request_actor"], decision["request_run_id"])
    live(decision)
    with store.transaction() as conn:
        _request(conn, decision)
        task = store.get(decision["task_id"], conn)
        if task.get("revision", 0) != decision["revision"] or payload_hash(task.get("payload", {})) != decision["payload_sha256"]:
            raise PolicyError("stale approval")
        body = canonical(envelope)
        prior = conn.execute("SELECT body FROM decisions WHERE id=?", (decision["id"],)).fetchone()
        if prior:
            if prior[0] != body:
                raise Conflict("409: decision id already bound")
            return decision
        conn.execute("INSERT INTO decisions VALUES(?,?,?,?)",
                     (decision["id"], "approval", decision["task_id"], body))
        store.event(conn, decision["task_id"], decision["actor"], "approval", decision)
    return decision


def validate_approval(store, conn, approval_id, task, bound_scope, actor, run_id, revision,
                      *, reason=None, recipient=None):
    row = conn.execute("SELECT kind,body FROM decisions WHERE id=?", (approval_id,)).fetchone()
    if row is None or row[0] != "approval":
        raise PolicyError("valid board approval required")
    envelope = json.loads(row[1])
    _verify_signature(store, envelope)
    decision = envelope["document"]
    request = _request(conn, decision)
    live(decision)
    independent(decision["actor"], decision["run_id"], actor, run_id)
    if (decision["task_id"] != task["id"] or decision["scope"] != bound_scope
            or decision["revision"] != revision or decision["request_actor"] != actor
            or decision["request_run_id"] != run_id
            or decision["payload_sha256"] != payload_hash(task.get("payload", {}))):
        raise PolicyError("approval binding mismatch or stale payload/revision")
    if reason is not None and (request["reason"] != reason or request["recipient"] != recipient):
        raise PolicyError("mutation differs from approved request")
    return decision


def insert_review(store, conn, task, actor, run_id, bound_scope, request_actor, request_run_id, reason):
    if actor != "menxia":
        raise PolicyError("only menxia can produce review_decision")
    independent(actor, run_id, request_actor, request_run_id)
    decision = {"id": uuid.uuid4().hex, "kind": "review_decision", "task_id": task["id"],
                "actor": actor, "run_id": run_id, "request_actor": request_actor,
                "request_run_id": request_run_id, "payload_sha256": payload_hash(task.get("payload", {})),
                "revision": task.get("revision", 0), "scope": bound_scope, "created_at": now(),
                "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
                "decision": "reject" if bound_scope["to"] == "Zhongshu" else "accept", "reason": reason}
    conn.execute("INSERT INTO decisions VALUES(?,?,?,?)",
                 (decision["id"], "review_decision", task["id"], canonical(decision)))
    store.event(conn, task["id"], actor, "review_decision", decision)
    return decision
