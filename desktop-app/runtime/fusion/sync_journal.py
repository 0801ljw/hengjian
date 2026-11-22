"""Local append-only sync journal for 衡鉴·点将台 (M0).

The journal is a local, single-writer fact log. It does not replace the
authoritative state machine yet: it is the contract substrate that the future
Hub/mobile sync will replicate. No network, credentials, private keys or blobs
are handled here.

Invariants:
- one row per mutation; duplicate mutation_id is idempotent;
- version strictly increases per (entity_type, entity_id);
- a tombstone is final; no later upsert;
- each row chains to the previous row in the same workspace;
- seq is assigned by this journal and is monotonic per workspace.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

GENESIS = "GENESIS"
OPS = {"upsert", "tombstone"}


class SyncError(ValueError):
    """A rejected journal mutation or a broken chain."""


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _require_text(name: str, value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 512:
        raise SyncError(f"{name} must be a nonempty string of at most 512 characters")
    return value


def _record_hash(record: Mapping[str, Any]) -> str:
    hashed = {k: record[k] for k in (
        "workspace_id", "device_id", "entity_type", "entity_id",
        "version", "mutation_id", "op", "payload", "prev_hash", "created_at")}
    material = record["prev_hash"] + "\n" + canonical(hashed)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class SyncJournal:
    """A small, dependency-free SQLite append-only journal."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS sync_events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    workspace_id TEXT NOT NULL,
                    device_id TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    mutation_id TEXT NOT NULL UNIQUE,
                    op TEXT NOT NULL CHECK (op IN ('upsert', 'tombstone')),
                    payload TEXT NOT NULL,
                    prev_hash TEXT NOT NULL,
                    hash TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS sync_events_workspace_seq
                    ON sync_events (workspace_id, seq);
                CREATE INDEX IF NOT EXISTS sync_events_entity
                    ON sync_events (workspace_id, entity_type, entity_id, seq);
                """
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _row(row: sqlite3.Row) -> dict:
        return {key: row[key] for key in row.keys()}

    def append(self, *, workspace_id: str, device_id: str, entity_type: str,
               entity_id: str, version: int, mutation_id: str, op: str,
               payload: Any) -> dict:
        workspace_id = _require_text("workspace_id", workspace_id)
        device_id = _require_text("device_id", device_id)
        entity_type = _require_text("entity_type", entity_type)
        entity_id = _require_text("entity_id", entity_id)
        mutation_id = _require_text("mutation_id", mutation_id)
        if op not in OPS:
            raise SyncError("op must be upsert or tombstone")
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise SyncError("version must be a positive integer")
        if isinstance(payload, str):
            payload_text = payload
        elif isinstance(payload, Mapping):
            payload_text = canonical(dict(payload))
        else:
            raise SyncError("payload must be a JSON object or string")

        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT * FROM sync_events WHERE mutation_id=?", (mutation_id,)
            ).fetchone()
            if existing is not None:
                conn.rollback()
                return self._row(existing)

            last = conn.execute(
                "SELECT hash FROM sync_events WHERE workspace_id=? ORDER BY seq DESC LIMIT 1",
                (workspace_id,),
            ).fetchone()
            prev_hash = last["hash"] if last is not None else GENESIS

            latest = conn.execute(
                "SELECT version, op FROM sync_events WHERE workspace_id=? AND entity_type=? "
                "AND entity_id=? ORDER BY seq DESC LIMIT 1",
                (workspace_id, entity_type, entity_id),
            ).fetchone()
            if latest is not None:
                if version <= latest["version"]:
                    raise SyncError("version must increase")
                if latest["op"] == "tombstone" and op == "upsert":
                    raise SyncError("entity is tombstoned")

            created_at = datetime.now(timezone.utc).isoformat()
            record = {
                "workspace_id": workspace_id,
                "device_id": device_id,
                "entity_type": entity_type,
                "entity_id": entity_id,
                "version": version,
                "mutation_id": mutation_id,
                "op": op,
                "payload": payload_text,
                "prev_hash": prev_hash,
                "created_at": created_at,
            }
            record["hash"] = _record_hash(record)
            cursor = conn.execute(
                "INSERT INTO sync_events (workspace_id, device_id, entity_type, entity_id, "
                "version, mutation_id, op, payload, prev_hash, hash, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (workspace_id, device_id, entity_type, entity_id, version, mutation_id,
                 op, payload_text, prev_hash, record["hash"], created_at),
            )
            seq = cursor.lastrowid
            conn.commit()
            record["seq"] = seq
            return record
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def after(self, workspace_id: str, cursor: int = 0, limit: int = 200) -> dict:
        workspace_id = _require_text("workspace_id", workspace_id)
        if isinstance(cursor, bool) or not isinstance(cursor, int) or cursor < 0:
            raise SyncError("cursor must be a nonnegative integer")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise SyncError("limit must be 1..1000")
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT * FROM sync_events WHERE workspace_id=? AND seq>? ORDER BY seq LIMIT ?",
                (workspace_id, cursor, limit + 1),
            ).fetchall()
        has_more = len(rows) > limit
        events = [self._row(row) for row in rows[:limit]]
        next_cursor = events[-1]["seq"] if events else cursor
        return {"events": events, "next_cursor": next_cursor, "has_more": has_more}

    def snapshot(self, workspace_id: str) -> dict:
        workspace_id = _require_text("workspace_id", workspace_id)
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT e.* FROM sync_events e JOIN ("
                "  SELECT entity_type, entity_id, MAX(seq) AS max_seq FROM sync_events "
                "  WHERE workspace_id=? GROUP BY entity_type, entity_id"
                ") latest ON e.seq = latest.max_seq WHERE e.workspace_id=? "
                "ORDER BY e.entity_type, e.entity_id",
                (workspace_id, workspace_id),
            ).fetchall()
            row = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) AS cursor FROM sync_events WHERE workspace_id=?",
                (workspace_id,),
            ).fetchone()
        return {"entities": [self._row(r) for r in rows], "cursor": row["cursor"]}

    def verify_chain(self, workspace_id: str) -> bool:
        workspace_id = _require_text("workspace_id", workspace_id)
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT * FROM sync_events WHERE workspace_id=? ORDER BY seq",
                (workspace_id,),
            ).fetchall()
        previous = GENESIS
        for row in rows:
            record = self._row(row)
            if record["prev_hash"] != previous:
                raise SyncError("broken prev_hash")
            if _record_hash(record) != record["hash"]:
                raise SyncError("broken record hash")
            previous = record["hash"]
        return True


def new_mutation_id() -> str:
    return str(uuid.uuid4())
