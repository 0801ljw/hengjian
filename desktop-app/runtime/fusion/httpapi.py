"""Transport-independent, authenticated API; importing this module starts no server.

Use API(Store(...)) for a trusted namespace, or the four-argument module handle
for fusion/var. tokens.json is {"tokens": [{"name": ..., "sha256": ...}]}.
Provision using add_token(name, high_entropy_token); plaintext is never saved.
POST queues work durably, without model calls. A separately authorized consumer
calls process_next(client_factory) with a DecisionClient factory. Budget/lease/
approval enforcement remains exclusively in the existing Pipeline/Store.
"""
from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import os
import re
import threading
import uuid
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import urlsplit

from . import budget
from .ai_pipeline import Pipeline
from .ai_view import read_runs, run_view
from .core import PolicyError, Store, atomic_json_write, now

IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{1,128}")
MAX_BODY = 65536


def worker_error_summary(exc):
    """Public diagnostic categories, never exception text or a traceback."""
    name = type(exc).__name__
    error_type = name if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", name) else "Exception"
    if isinstance(exc, budget.BudgetError) and exc.code == "UNPRICED_BLOCKED":
        message = "Unpriced usage is disabled; review worker configuration and budget policy."
    elif isinstance(exc, PolicyError):
        message = "Execution refused by policy; review admission and role configuration."
    elif isinstance(exc, OSError):
        message = "Worker I/O failed; check local service availability."
    else:
        message = "Worker execution failed; operator investigation required."
    return {"error_type": error_type, "message": message, "at": now()}


class API:
    def __init__(self, store: Store):
        self.store = store
        self.tokens = store.home / "tokens.json"
        self.audit = store.home / "api-audit.jsonl"
        self.queue = store.home / "requests"
        self._ephemeral = {}

    def add_token(self, name: str, token: str) -> None:
        """Trusted local provisioning only; not an HTTP endpoint. No defaults."""
        if (not isinstance(name, str) or not IDENTIFIER.fullmatch(name)
                or not isinstance(token, str) or len(token) < 32
                or len(token) > 512 or not token.isascii() or any(c.isspace() for c in token)):
            raise ValueError("use a named, high-entropy token of 32..512 ASCII characters")
        digest = hashlib.sha256(token.encode()).hexdigest()
        with (self.store.home / "tokens-provision.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            records = self._token_records() if self.tokens.exists() else []
            if any(r["name"] == name or r["sha256"] == digest for r in records):
                raise ValueError("token name or digest already exists")
            atomic_json_write(self.tokens, {"tokens": records + [{"name": name, "sha256": digest}]})
            self.tokens.chmod(0o600)

    def add_ephemeral_token(self, name: str, token: str) -> None:
        """Process-local credential for session delegation; never persisted."""
        if (not isinstance(name, str) or not IDENTIFIER.fullmatch(name)
                or not isinstance(token, str) or len(token) < 32
                or not token.isascii() or any(c.isspace() for c in token)):
            raise ValueError("invalid ephemeral token")
        self._ephemeral[name] = hashlib.sha256(token.encode()).hexdigest()

    def _token_records(self):
        if not self.tokens.exists():
            return []
        if self.tokens.is_symlink():
            raise ValueError("invalid token configuration")
        data = json.loads(self.tokens.read_text())
        records = data["tokens"]
        if not isinstance(records, list):
            raise ValueError("invalid token configuration")
        names, hashes = set(), set()
        for record in records:
            if (not isinstance(record, dict) or set(record) != {"name", "sha256"}
                    or not isinstance(record["name"], str)
                    or not IDENTIFIER.fullmatch(record["name"])
                    or not isinstance(record["sha256"], str)
                    or not re.fullmatch(r"[0-9a-f]{64}", record["sha256"])
                    or record["name"] in names or record["sha256"] in hashes):
                raise ValueError("invalid token configuration")
            names.add(record["name"])
            hashes.add(record["sha256"])
        return records

    def _authenticate(self, headers):
        try:
            if not isinstance(headers, Mapping):
                return None
            values = [v for k, v in headers.items() if isinstance(k, str) and k.lower() == "authorization"]
            if len(values) != 1 or not isinstance(values[0], str):
                return None
            parts = values[0].split()
            if (len(parts) != 2 or parts[0].lower() != "bearer"
                    or not 32 <= len(parts[1]) <= 512 or not parts[1].isascii()):
                return None
            digest = hashlib.sha256(parts[1].encode()).hexdigest()
            matched = None
            for record in self._token_records():
                if hmac.compare_digest(record["sha256"], digest):
                    matched = record["name"]
            for name, ephemeral_digest in self._ephemeral.items():
                if hmac.compare_digest(ephemeral_digest, digest):
                    return name
            return matched
        except (OSError, ValueError, TypeError, KeyError):
            return None

    def _audit_request(self, stream, who, what, status):
        # No caller-controlled query strings, headers, body, or exception text.
        record = {"who": who or "anonymous", "what": what, "status": status, "time": now()}
        fcntl.flock(stream, fcntl.LOCK_EX)
        try:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)

    @staticmethod
    def _route(method, path):
        if not isinstance(method, str) or not isinstance(path, str) or len(path) > 2048:
            return None, None
        method = method.upper()
        try:
            parsed = urlsplit(path)
        except ValueError:
            return None, None
        if parsed.scheme or parsed.netloc or not path.startswith("/"):
            return None, None
        path = parsed.path
        if path in {"/api/health", "/api/tasks", "/api/requests", "/api/budget", "/api/merit"}:
            return path, None
        match = re.fullmatch(r"/api/(tasks|runs)/([A-Za-z0-9_-]{1,128})", path)
        if match:
            return "/api/" + match[1] + "/<id>", match[2]
        return None, None

    def handle(self, method, path, headers, body):
        """Return (HTTP status, JSON-compatible object), auditing every attempt.

        Audit open failure prevents dispatch. A later audit write failure returns
        503, but an already queued request is not rolled back. Reliable storage
        is required for a durable per-request audit guarantee.
        """
        who = self._authenticate(headers)
        route, key = None, None
        status, result = 500, {"error": "internal error"}
        try:
            # Open before dispatch: an unavailable audit sink must not enqueue.
            flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW
            with os.fdopen(os.open(self.audit, flags, 0o600), "a") as stream:
                try:
                    route, key = self._route(method, path)
                    if who is None:
                        status, result = 401, {"error": "unauthorized"}
                    else:
                        status, result = self._dispatch(method.upper(), route, key, body, who)
                except Exception:
                    # Transport trust boundary: never send internal details.
                    status, result = 500, {"error": "internal error"}
                what = (method.upper() if isinstance(method, str) and method.upper() in
                        {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"} else "OTHER")
                self._audit_request(stream, who, what + " " + (route or "unmatched"), status)
        except OSError:
            return 503, {"error": "service unavailable"}
        return status, result

    def _dispatch(self, method, route, key, body, who):
        if route is None:
            return 404, {"error": "not found"}
        if method != ("POST" if route == "/api/requests" else "GET"):
            return 405, {"error": "method not allowed"}
        if route == "/api/health":
            return 200, {"ok": True}
        if route == "/api/budget":
            return 200, budget.ledger(self.store)
        if route == "/api/merit":
            from .merit import merit_view
            return 200, merit_view(self.store.snapshot())
        if route == "/api/tasks":
            return 200, {"tasks": self.store.snapshot()["tasks"]}
        if route == "/api/tasks/<id>":
            task = next((t for t in self.store.snapshot()["tasks"] if t["id"] == key), None)
            return (200, task) if task else (404, {"error": "not found"})
        if route == "/api/runs/<id>":
            return self._get_run(key)
        try:
            if isinstance(body, (str, bytes)):
                if len(body) > MAX_BODY:
                    return 413, {"error": "request too large"}
                body = json.loads(body)
            if (not isinstance(body, dict) or set(body) != {"request"}
                    or not isinstance(body["request"], str) or not body["request"].strip()
                    or len(body["request"]) > 12000):
                return 400, {"error": "expected request text (1..12000 characters)"}
        except (ValueError, UnicodeError):
            return 400, {"error": "invalid JSON"}
        self.queue.mkdir(mode=0o700, exist_ok=True)
        key = "AI-" + uuid.uuid4().hex
        atomic_json_write(self.queue / (key + ".json"), {
            "id": key, "status": "queued", "who": who, "request": body["request"], "time": now()})
        return 200, {"id": key, "status": "queued", "run_url": "/api/runs/" + key}

    def _get_run(self, key):
        queued = self.queue / (key + ".json")
        submission = json.loads(queued.read_text()) if queued.is_file() else None
        for view in read_runs(self.store):
            if view["id"] == key or any(run["id"] == key for run in view["runs"]):
                if submission and submission["status"] in {"queued", "running", "error"}:
                    view["status"] = submission["status"]
                return 200, view
        if submission:
            view = run_view(self.store.snapshot(), [], key)
            view["status"] = submission["status"]
            return 200, view
        snapshot = self.store.snapshot()
        run = next((r for r in snapshot["runs"] if r["id"] == key), None)
        if run:
            return 200, run_view(snapshot, [], run["task_id"])
        return 404, {"error": "not found"}

    def process_next(self, client_factory, *, allow_unpriced=False):
        """Explicit trusted consumer, outside handle; never auto-replay running work.

        The factory must return a DecisionClient-compatible client. Production
        callers must independently authorize provider usage. A crashed 'running'
        entry requires operator reconciliation, not a duplicate execution.
        """
        self.queue.mkdir(mode=0o700, exist_ok=True)
        with (self.queue / "consumer.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return None
            for path in sorted(self.queue.glob("AI-*.json")):
                item = json.loads(path.read_text())
                if item["status"] != "queued":
                    continue
                item.update(status="running", started_at=now())
                atomic_json_write(path, item)
                try:
                    client = client_factory(self.store.home / "ai" / item["id"])
                    result = Pipeline(self.store, client, item["id"]).run(
                        item["request"], allow_unpriced=allow_unpriced)
                    item.update(status=result["status"])
                except Exception as exc:
                    item.update(status="error", result={"error": worker_error_summary(exc)})
                item["ended_at"] = now()
                atomic_json_write(path, item)
                response = {"id": item["id"], "status": item["status"]}
                if item.get("result", {}).get("error"):
                    response["error"] = item["result"]["error"]
                return response
        return None


_default_api = None
_default_lock = threading.Lock()


def handle(method, path, headers, body):
    """Default local namespace. Use API(store).handle for explicit configuration."""
    global _default_api
    with _default_lock:
        if _default_api is None:
            _default_api = API(Store(Path(__file__).resolve().parent / "var"))
    return _default_api.handle(method, path, headers, body)
