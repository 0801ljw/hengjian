"""Read-only projection of Fusion's ledger into the M0 sync journal.

M1-a deliberately does not change the state machine, open a network socket or
encrypt payloads. It copies current tasks and their decision events into the
append-only journal with deterministic mutation ids, so a later Hub/client can
replay a cursor and reconstruct the same case view.

Run against a copy of an instance; the projection opens the business database
in read-only mode.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from .core import Store
from .sync_journal import SyncJournal


def project_store(store: Store, journal: SyncJournal, *,
                  workspace_id: str = "w_local", device_id: str = "d_local") -> dict:
    """Project events and current task snapshots; idempotent by mutation id."""
    db = Path(store.db)
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    events = tasks = 0
    try:
        for row in conn.execute(
                "SELECT seq, task_id, at, actor, action, detail FROM events ORDER BY seq").fetchall():
            try:
                detail = json.loads(row["detail"])
            except (TypeError, ValueError):
                detail = {"raw": str(row["detail"])}
            journal.append(
                workspace_id=workspace_id,
                device_id=device_id,
                entity_type="event",
                entity_id=f'{row["task_id"]}:{row["seq"]}',
                version=1,
                mutation_id=f'evt:{row["task_id"]}:{row["seq"]}',
                op="upsert",
                payload={
                    "seq": row["seq"], "task_id": row["task_id"], "at": row["at"],
                    "actor": row["actor"], "action": row["action"], "detail": detail,
                },
            )
            events += 1

        for row in conn.execute("SELECT id, body FROM tasks ORDER BY id").fetchall():
            try:
                task = json.loads(row["body"])
            except (TypeError, ValueError):
                continue
            revision = task.get("revision", 0)
            if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
                revision = 0
            journal.append(
                workspace_id=workspace_id,
                device_id=device_id,
                entity_type="case",
                entity_id=row["id"],
                version=revision + 1,
                mutation_id=f'case:{row["id"]}:{revision}',
                op="upsert",
                payload={
                    "id": row["id"], "state": task.get("state"),
                    "revision": revision, "title": task.get("title"),
                    "updated_at": task.get("updatedAt"),
                },
            )
            tasks += 1
    finally:
        conn.close()

    snapshot = journal.snapshot(workspace_id)
    return {
        "workspace_id": workspace_id,
        "cursor": snapshot["cursor"],
        "events_projected": events,
        "cases_projected": tasks,
        "journal": str(journal.path),
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="fusion sync-project",
        description="Read-only M1-a projection of tasks/events into the sync journal")
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--journal", type=Path, default=None)
    parser.add_argument("--workspace", default="w_local")
    parser.add_argument("--device", default="d_local")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    store = Store(args.home)
    journal = SyncJournal(args.journal or (args.home / "sync-journal.sqlite3"))
    result = project_store(store, journal, workspace_id=args.workspace, device_id=args.device)
    print(json.dumps(result, ensure_ascii=False, indent=2) if args.json
          else f"{result['workspace_id']}: cursor={result['cursor']} "
               f"events={result['events_projected']} cases={result['cases_projected']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
