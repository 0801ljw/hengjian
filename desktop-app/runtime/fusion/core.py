"""Single mutation service: canonical rules, transactional ledger, projection."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import sys
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import kanban_update as kb  # noqa: E402
from file_lock import atomic_json_write  # noqa: E402


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class PolicyError(ValueError):
    pass


class Conflict(PolicyError):
    """409 semantics: an exclusive execution already exists."""
    status_code = 409


def authorize(actor: str, command: str) -> None:
    """Frozen matrix only: this is the upstream contract and must not widen."""
    policy = kb.AGENT_POLICY.get(actor)
    if not policy or command not in policy["commands"]:
        raise PolicyError(f"permission denied: {actor}/{command}")


def authorize_effective(actor: str, command: str) -> None:
    """Frozen matrix plus the narrow grants declared in fusion/policy.py.

    Only the cancellation flow uses this, and only for `cancel` / `confirm`,
    which policy.check_cancel / the PendingConfirm owner check further bound.
    """
    policy = kb.AGENT_POLICY.get(actor)
    if policy and command in policy["commands"]:
        return
    from . import policy as _policy
    if command in ("cancel", "confirm") and _policy.granted(actor, command):
        return
    raise PolicyError(f"permission denied: {actor}/{command}")


def check_transition(old: str, new: str) -> None:
    if new not in kb._VALID_TRANSITIONS.get(old, set()):
        raise PolicyError(f"illegal transition: {old}->{new}")


def check_edge(actor: str, target: str) -> None:
    agents = json.loads((ROOT / "agents.json").read_text())
    matrix = {a["id"]: a.get("subagents", {}).get("allowAgents", []) for a in agents}
    if target not in matrix.get(actor, []):
        raise PolicyError(f"communication denied: {actor}->{target}")


def company_home(home: Path, company: str) -> Path:
    """Trusted local controller namespace, not an authentication credential.

    Default retains the pre-P3 on-disk layout. Named companies never fall back
    to the default database. Reject symlinks at the namespace boundary.
    """
    if not isinstance(company, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", company) is None:
        raise PolicyError("invalid company identifier")
    root = home.absolute()
    if root.is_symlink():
        raise PolicyError("company home cannot be a symlink")
    root = root.resolve()
    selected = root if company == "default" else root / "companies" / company
    if (root / "companies").is_symlink() or selected.is_symlink():
        raise PolicyError("company namespace cannot be a symlink")
    if (selected / "fusion.sqlite3").is_symlink():
        raise PolicyError("company database cannot be a symlink")
    return selected


class Store:
    def __init__(self, home: Path, board_key_file: Path | None = None, *, company="default"):
        self.company = company
        self.instance_home = home.resolve()
        self.home = company_home(home, company)
        self.home.mkdir(parents=True, exist_ok=True)
        self.db = self.home / "fusion.sqlite3"
        self.board_key_file = board_key_file
        self._fences = {}
        with self.connect() as conn:
            conn.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY, body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events(
                    seq INTEGER PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id),
                    at TEXT NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL,
                    detail TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS runs(
                    id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id),
                    status TEXT NOT NULL, body TEXT NOT NULL);
                CREATE UNIQUE INDEX IF NOT EXISTS one_running_task
                    ON runs(task_id) WHERE status='running';
                CREATE TABLE IF NOT EXISTS mutation_requests(
                    id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id),
                    body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS decisions(
                    id TEXT PRIMARY KEY, kind TEXT NOT NULL CHECK(kind IN ('approval','review_decision')),
                    task_id TEXT NOT NULL REFERENCES tasks(id), body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS approval_uses(
                    approval_id TEXT PRIMARY KEY REFERENCES decisions(id),
                    fingerprint TEXT NOT NULL, result TEXT NOT NULL);
            """)
            from .budget import initialize
            initialize(conn)
            from .leases import initialize as initialize_leases
            initialize_leases(conn)
        from .budget import recover
        recover(self)

    def connect(self):
        conn = sqlite3.connect(self.db, timeout=5)
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    @contextmanager
    def transaction(self):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def event(conn, task_id, actor, action, detail):
        conn.execute("INSERT INTO events(task_id,at,actor,action,detail) VALUES(?,?,?,?,?)",
                     (task_id, now(), actor, action, json.dumps(detail, ensure_ascii=False)))

    def create(self, task_id: str, title: str):
        from .mutate import apply_mutation, payload_hash
        return apply_mutation(self, task_id, "taizi", "create", "Taizi", title,
                              payload={"title": title}, payload_sha256=payload_hash({"title": title}),
                              expected_revision=0, run_id="local:taizi")

    def get(self, task_id: str, conn=None) -> dict:
        if conn is None:
            with self.connect() as connection:
                return self.get(task_id, connection)
        row = conn.execute("SELECT body FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise PolicyError(f"missing task: {task_id}")
        return json.loads(row[0])

    def move(self, task_id: str, actor: str, target: str, reason: str,
             command: str = "state", recipient: str | None = None, **kwargs):
        from .mutate import apply_mutation, payload_hash
        task = self.get(task_id)
        kwargs.setdefault("expected_revision", task.get("revision", 0))
        kwargs.setdefault("payload_sha256", payload_hash(task.get("payload", {})))
        kwargs.setdefault("run_id", f"local:{actor}")
        kwargs.setdefault("fencing_token", self._fences.get(task_id, 0))
        return apply_mutation(self, task_id, actor, command, target, reason,
                              recipient=recipient, **kwargs)

    def checkout(self, task_id: str, allow_unpriced: bool = False, *,
                 provider="penguinapi", estimated_cents=None, run_id=None,
                 budget_override=None) -> str:
        from .mutate import apply_mutation, payload_hash
        authorize("gongbu", "progress")
        task = self.get(task_id)
        run_id = run_id or uuid.uuid4().hex
        from .budget import flush_denials
        try:
            apply_mutation(self, task_id, "shangshu", "dispatch", "Doing", "runtime admission",
                           expected_revision=task["revision"], payload_sha256=payload_hash(task["payload"]),
                           run_id=run_id, payload={"provider": provider, "estimated_cents": estimated_cents,
                                                   "allow_unpriced": allow_unpriced,
                                                   "budget_override": budget_override})
        except sqlite3.IntegrityError as exc:
            raise Conflict("409: task already has a running execution") from exc
        except BaseException:
            # 预算拒绝等失败：事务已回滚，这里把拒绝原因补写进账本
            flush_denials(self)
            raise
        flush_denials(self)
        return run_id

    def finish(self, run_id: str, result: dict, *, fencing_token=None):
        from .leases import check, held_token, release, stop_heartbeat
        if fencing_token is None:
            fencing_token = held_token(self, run_id)
        with self.transaction() as conn:
            row = conn.execute("SELECT task_id,status,body FROM runs WHERE id=?", (run_id,)).fetchone()
            if row is None:
                raise Conflict("run is missing or already finalized")
            task_id, _, body = row
            check(conn, task_id, fencing_token, active=True, owner_id=run_id)
            if row[1] != "running":
                raise Conflict("run is missing or already finalized")
            data = json.loads(body)
            if set(result) & {"id", "task_id", "agent", "provider", "pricing", "estimated_cents",
                              "started_at", "ended_at", "unknown_spend", "fencing_token"}:
                raise PolicyError("runtime result cannot replace admission metadata")
            data.update(result, ended_at=now())
            if result["status"] not in {"succeeded", "failed", "timeout", "unavailable"}:
                raise PolicyError("invalid final runtime status")
            from .budget import settle
            data.update(settle(conn, run_id, result, fencing_token=fencing_token))
            if data.get("pricing") == "unpriced":
                data["cost_usd"] = None
            conn.execute("UPDATE runs SET status=?,body=? WHERE id=?",
                         (result["status"], json.dumps(data), run_id))
            self.event(conn, task_id, "gongbu", "run_finished", data)
            if result["status"] == "succeeded":
                task = self.get(task_id, conn)
                from .mutate import apply_mutation, payload_hash
                apply_mutation(self, task_id, "gongbu", "output", task["state"], "runtime artifact",
                               payload={**task.get("payload", {}), "output": result["artifact"]},
                               payload_sha256=payload_hash(task.get("payload", {})),
                               expected_revision=task.get("revision", 0), run_id=run_id,
                               fencing_token=fencing_token, conn=conn)
                if conn.execute("SELECT 1 FROM execution_commits WHERE task_id=?", (task_id,)).fetchone():
                    raise Conflict("task result already committed; duplicate side effect refused")
                conn.execute("INSERT INTO execution_commits VALUES(?,?,?,?)",
                             (task_id, run_id, fencing_token, json.dumps(result["artifact"])))
            release(conn, task_id, run_id, fencing_token)
        stop_heartbeat(self, run_id)
        self.project()

    def budget_status(self, agent="gongbu", month=None):
        from .budget import status
        with self.connect() as conn:
            return status(conn, agent, month)

    def snapshot(self) -> dict:
        with self.connect() as conn:
            tasks = [json.loads(row[0]) for row in conn.execute("SELECT body FROM tasks ORDER BY id")]
            runs = [dict(json.loads(row[1]), status=row[0]) for row in conn.execute("SELECT status,body FROM runs")]
            events = [{"seq": row[0], "task_id": row[1], "at": row[2], "actor": row[3],
                       "action": row[4], "detail": json.loads(row[5])}
                      for row in conn.execute("SELECT * FROM events ORDER BY seq")]
            decisions = []
            for row in conn.execute("SELECT body FROM decisions ORDER BY id"):
                record = json.loads(row[0])
                decisions.append(record.get("document", record))
        return {"tasks": tasks, "runs": runs, "events": events, "decisions": decisions}

    def project(self):
        # One writer lock covers snapshot+projection, avoiding stale concurrent snapshots.
        with self.transaction() as conn:
            tasks = [json.loads(row[0]) for row in conn.execute("SELECT body FROM tasks ORDER BY id")]
            atomic_json_write(self.home / "tasks_source.json", tasks)
            atomic_json_write(self.home / "live_status.json",
                              {"generatedAt": now(), "tasks": tasks, "source": "fusion-sqlite",
                               "agents": [], "stats": {}, "syncStatus": {}})


def artifact(path: Path) -> dict:
    return {"path": str(path.resolve()), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "bytes": path.stat().st_size}
