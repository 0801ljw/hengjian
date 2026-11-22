"""Versioned logical company backup. No SQL, keys or executable archive paths."""
from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import sqlite3
import tarfile
import tempfile
from pathlib import Path
from contextlib import closing

from .core import PolicyError, Store, authorize_effective, check_transition, company_home, kb
from .mutate import canonical

VERSION = 1
MAX_BYTES = 128 * 1024 * 1024
TABLES = (
    "tasks", "events", "runs", "mutation_requests", "decisions", "approval_uses",
    "budget_policy", "budget_reservations", "budget_events", "budget_overrides",
    "leases", "execution_commits",
)
EXCLUDED = ["active leases/running runs/reserved budgets (export refused)",
            "process capabilities/heartbeat threads", "SQLite WAL/SHM and temporary files",
            "JSON projections (rebuilt)", "private/public keys (configure trusted public key separately)",
            "external artifacts, model transcripts and files outside the company database",
            "other companies"]


def _columns(conn, table):
    return [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')]


def read_state(conn):
    return {table: {"columns": _columns(conn, table),
                    "rows": [list(row) for row in conn.execute(f'SELECT * FROM "{table}" ORDER BY 1')]}
            for table in TABLES}


def _quiescent(conn):
    for query in ("SELECT 1 FROM leases WHERE owner_id IS NOT NULL",
                  "SELECT 1 FROM runs WHERE status='running'",
                  "SELECT 1 FROM budget_reservations WHERE status='reserved'"):
        if conn.execute(query).fetchone():
            raise PolicyError("export/import requires quiescent company: active execution or reservation")


def export_bundle(home: Path, out: Path, *, company="default", board_key_file=None):
    selected = company_home(home, company)
    if not (selected / "fusion.sqlite3").is_file():
        raise PolicyError("company database does not exist")
    # Read-only transaction: never run startup recovery as a backup side effect.
    if out.resolve().is_relative_to(selected):
        raise PolicyError("bundle output must be outside the company home")
    with closing(sqlite3.connect((selected / "fusion.sqlite3").as_uri() + "?mode=ro", uri=True)) as conn:
        conn.execute("BEGIN")
        _quiescent(conn)
        state = read_state(conn)
        schema_hash = _schema_hash(conn)
        conn.rollback()
    data = canonical(state).encode()
    manifest = {"format": "fusion-company", "schema_version": VERSION, "company": company,
                "schema_sha256": schema_hash,
                "state_sha256": hashlib.sha256(data).hexdigest(),
                "record_counts": {t: len(v["rows"]) for t, v in state.items()},
                "excluded": EXCLUDED}
    out = out.absolute()
    out.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".fusion-export-", dir=out.parent)
    try:
        os.fchmod(fd, 0o600)
        os.close(fd)
        with tarfile.open(temporary, "w:gz") as archive:
            for name, body in (("manifest.json", canonical(manifest).encode()), ("state.json", data)):
                info = tarfile.TarInfo(name)
                info.size, info.mode = len(body), 0o600
                archive.addfile(info, io.BytesIO(body))
        os.replace(temporary, out)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return manifest


def _schema_hash(conn):
    schema = conn.execute("SELECT type,name,tbl_name,sql FROM sqlite_master "
                          "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name").fetchall()
    return hashlib.sha256(canonical(schema).encode()).hexdigest()


def _read_bundle(bundle):
    try:
        with tarfile.open(bundle, "r:gz") as archive:
            # Stream bounded members; never extract a path, symlink, device or SQL.
            parts = {}
            for member in archive:
                if (member.name not in {"manifest.json", "state.json"} or member.name in parts
                        or not member.isfile() or not 0 < member.size <= MAX_BYTES):
                    raise PolicyError("invalid archive member")
                parts[member.name] = archive.extractfile(member).read(MAX_BYTES + 1)
            if set(parts) != {"manifest.json", "state.json"}:
                raise PolicyError("missing bundle members")
        from .ai import strict_json
        manifest = strict_json(parts["manifest.json"])
        if (manifest["format"] != "fusion-company" or type(manifest["schema_version"]) is not int
                or manifest["schema_version"] != VERSION):
            raise PolicyError("unsupported bundle schema version")
        if hashlib.sha256(parts["state.json"]).hexdigest() != manifest["state_sha256"]:
            raise PolicyError("bundle integrity hash mismatch")
        state = strict_json(parts["state.json"])
        if set(state) != set(TABLES) or manifest["record_counts"] != {
                t: len(state[t]["rows"]) for t in TABLES}:
            raise PolicyError("bundle record count mismatch")
        return manifest, state
    except (tarfile.TarError, EOFError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise PolicyError(f"invalid bundle: {exc}") from exc


def _validate(store, conn):
    """Verify archived signatures/bindings without renewing expired approvals."""
    from .approvals import _request, _verify_signature, independent
    _quiescent(conn)
    if conn.execute("PRAGMA foreign_key_check").fetchall() or conn.execute("PRAGMA integrity_check").fetchone() != ("ok",):
        raise PolicyError("database integrity violation")
    approvals = {}
    for ident, kind, task_id, body in conn.execute("SELECT * FROM decisions"):
        value = json.loads(body)
        document = value["document"] if kind == "approval" else value
        if document["id"] != ident or document["kind"] != kind or document["task_id"] != task_id:
            raise PolicyError("decision identity mismatch")
        if kind == "approval":
            _verify_signature(store, value)
            _request(conn, document)
            independent(document["actor"], document["run_id"], document["request_actor"], document["request_run_id"])
            if (document["principal"] != "board" or document["decision"] != "approve"
                    or not document["actor"].startswith("board:")):
                raise PolicyError("invalid archived approval")
            approvals[ident] = document
    for (body,) in conn.execute("SELECT body FROM budget_policy UNION ALL SELECT body FROM budget_overrides"):
        _verify_signature(store, json.loads(body))
    for (body,) in conn.execute("SELECT body FROM budget_events WHERE kind IN ('policy_changed','override')"):
        _verify_signature(store, json.loads(body))
    uses = {}
    for ident, fingerprint, body in conn.execute("SELECT * FROM approval_uses"):
        result = json.loads(body)
        pending = result.get("pending_confirm", {})
        approval = approvals.get(ident)
        if (not approval or result["id"] != approval["task_id"] or pending.get("approval_id") != ident
                or pending.get("scope") != approval["scope"]
                or pending.get("request_actor") != approval["request_actor"]
                or pending.get("request_run_id") != approval["request_run_id"]
                or pending.get("request_revision") != approval["revision"]
                or result["revision"] != approval["revision"] + 1):
            raise PolicyError("broken approval consumption chain")
        uses[(result["id"], result["revision"])] = pending
    for ident, body in conn.execute("SELECT * FROM tasks"):
        task = json.loads(body)
        if task["id"] != ident:
            raise PolicyError("task identity mismatch")
        events = conn.execute("SELECT action,actor,detail FROM events WHERE task_id=? AND action IN "
                              "('create','state','done','confirm','payload','output','block','cancel') ORDER BY seq", (ident,)).fetchall()
        if not events or json.loads(events[-1][2])["to"] != task["state"] or json.loads(events[-1][2])["revision"] != task["revision"]:
            raise PolicyError("task/audit state mismatch")
        previous_state, previous_revision = None, 0
        for action, actor, detail in events:
            event = json.loads(detail)
            if event["from"] != previous_state or event["revision"] != previous_revision + 1:
                raise PolicyError("broken mutation audit chain")
            authorize_effective(actor, "progress" if action in {"payload", "output"} else action)
            if action == "create":
                if previous_state is not None or event["to"] != "Taizi" or actor != "taizi":
                    raise PolicyError("invalid archived task creation")
            elif action in {"payload", "output"}:
                if event["from"] != event["to"]:
                    raise PolicyError("payload cannot change archived state")
            elif event["to"] == "PendingConfirm":
                use = uses.get((ident, event["revision"]))
                if (use is None or use["scope"]["from"] != event["from"]
                        or use["scope"]["command"] != action
                        or (event["from"], use["target_state"]) not in kb.HIGH_RISK_TRANSITIONS
                        or use["request_actor"] != actor or use["request_run_id"] != event["run_id"]):
                    raise PolicyError("high-risk action without approval chain")
                decision = approvals[use["approval_id"]]
                if event["payload_sha256"] != decision["payload_sha256"]:
                    raise PolicyError("approval payload binding mismatch")
            else:
                check_transition(event["from"], event["to"])
            if event["from"] == "PendingConfirm":
                pending_use = uses.get((ident, previous_revision))
                if (pending_use is None or pending_use["target_state"] != event["to"] or action != "confirm"
                        or actor != kb.CONFIRM_AUTHORITY[pending_use["scope"]["from"]]):
                    raise PolicyError("confirmation without approval chain")
            if (event["from"], event["to"]) in kb.HIGH_RISK_TRANSITIONS:
                raise PolicyError("high-risk transition bypassed PendingConfirm")
            previous_state, previous_revision = event["to"], event["revision"]
        pending = task.get("pending_confirm")
        if task["state"] == "PendingConfirm" and pending != uses.get((ident, task["revision"])):
            raise PolicyError("missing pending approval")
    for run_id, task_id, status, body in conn.execute("SELECT * FROM runs"):
        run = json.loads(body)
        reservation = conn.execute("SELECT actual_cents,unknown_spend,status FROM budget_reservations WHERE run_id=?", (run_id,)).fetchone()
        if (run["id"] != run_id or run["task_id"] != task_id or run["status"] != status
                or reservation is None or reservation[2] == "reserved"
                or run.get("actual_cents") != reservation[0]
                or bool(run["unknown_spend"]) != bool(reservation[1])):
            raise PolicyError("run/budget ledger mismatch")


def import_bundle(bundle: Path, home: Path, *, company="default", board_key_file=None):
    """Build in a sibling directory; publish with one rename after all checks."""
    # Entire destination instance must be empty. Never merge/overwrite tenants.
    home = home.absolute()
    if home.is_symlink() or (home.exists() and (not home.is_dir() or any(home.iterdir()))):
        raise PolicyError("import requires an empty home directory")
    company_home(home, company)  # validate before creating anything
    manifest, state = _read_bundle(bundle)
    if manifest["company"] != company:
        raise PolicyError("bundle company mismatch; company remapping is not supported")
    home.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".fusion-import-", dir=home.parent))
    try:
        os.chmod(stage, 0o700)
        store = Store(stage, board_key_file, company=company)
        with store.transaction() as conn:
            if manifest["schema_sha256"] != _schema_hash(conn):
                raise PolicyError("database schema mismatch")
            conn.execute("PRAGMA defer_foreign_keys=ON")
            for table in TABLES:
                columns = _columns(conn, table)
                if state[table]["columns"] != columns:
                    raise PolicyError("table schema mismatch")
                rows = state[table]["rows"]
                if any(not isinstance(row, list) or len(row) != len(columns) for row in rows):
                    raise PolicyError("invalid record shape")
                conn.executemany(f'INSERT INTO "{table}" VALUES({",".join("?" for _ in columns)})', rows)
            _validate(store, conn)
            if read_state(conn) != state:
                raise PolicyError("imported state differs from bundle")
        store.project()
        with closing(store.connect()) as conn:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        # rmdir refuses a concurrent nonempty destination; rename never merges.
        if home.exists():
            home.rmdir()
        os.rename(stage, home)
        return manifest
    except (sqlite3.Error, ValueError, KeyError, TypeError) as exc:
        raise PolicyError(f"import rejected: {exc}") from exc
    finally:
        if stage.exists():
            shutil.rmtree(stage)
