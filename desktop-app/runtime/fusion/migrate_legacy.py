"""Import legacy kanban JSON (data/tasks_source.json) into a fusion instance.

The old system kept tasks in a JSON list. P1 deliberately left that unported, so
users with existing boards had no path forward. This migrator is explicit,
validating and transactional:

  fusion migrate-legacy --from data/ --home <instance> [--dry-run]

Rules:
  * every record must have id/title/state and a state the state machine knows
  * tasks are replayed through the public Store API (create + move), so the
    resulting rows satisfy the same invariants as natively created ones
  * a task whose target state is unreachable through the linear flow is
    reported as skipped with a reason, never silently dropped
  * the sqlite file is copied before writing and restored on any failure, so a
    failed import leaves the instance exactly as it was
  * --dry-run validates and prints the plan without writing
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import kanban_update as kb
from .core import PolicyError, Store
from .mutate import _owner as owner_of

FLOW = ["Pending", "Taizi", "Zhongshu", "Menxia", "Assigned", "Doing", "Review", "Done"]
TERMINAL = {"Done", "Cancelled", "Blocked"}
# actor that owns each state in the legacy flow
# The actor who may move a task OUT of a state is that state's owner.
STEP_COMMAND = {("Doing", "Review"): "done"}


class LegacyError(Exception):
    pass


def load_legacy(source: Path) -> list[dict]:
    path = source if source.is_file() else source / "tasks_source.json"
    if not path.is_file():
        raise LegacyError(f"legacy tasks file not found: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise LegacyError(f"legacy file is not valid JSON: {exc}") from exc
    if not isinstance(data, list):
        raise LegacyError(f"expected a JSON list, got {type(data).__name__}")
    return data


def plan(records: list[dict]) -> tuple[list[dict], list[dict]]:
    """Split into importable tasks and skipped ones, with explicit reasons."""
    known = set(FLOW) | set(kb._VALID_TRANSITIONS) | {"Pending", "Blocked", "Cancelled"}
    importable, skipped = [], []
    seen = set()
    for index, rec in enumerate(records):
        if not isinstance(rec, dict):
            skipped.append({"index": index, "reason": "record is not an object"})
            continue
        task_id = rec.get("id")
        title = rec.get("title")
        state = rec.get("state")
        if not isinstance(task_id, str) or not task_id.strip():
            skipped.append({"index": index, "reason": "missing id"})
            continue
        if task_id in seen:
            skipped.append({"index": index, "id": task_id, "reason": "duplicate id"})
            continue
        seen.add(task_id)
        if not isinstance(title, str) or not title.strip():
            skipped.append({"index": index, "id": task_id, "reason": "missing title"})
            continue
        if state not in known:
            skipped.append({"index": index, "id": task_id,
                            "reason": f"unknown state {state!r}"})
            continue
        if state in TERMINAL:
            # Importing an approved/terminal task would bypass the board decision
            # that produced it. Keep the record for the operator instead.
            skipped.append({"index": index, "id": task_id, "state": state,
                            "reason": "terminal state archived, not imported",
                            "record": rec})
            continue
        if state not in FLOW:
            skipped.append({"index": index, "id": task_id,
                            "reason": f"state {state} not reachable through the linear flow"})
            continue
        importable.append({"id": task_id, "title": title, "state": state,
                           "org": rec.get("org"), "now": rec.get("now")})
    return importable, skipped


def _snapshot(store: Store) -> list[Path]:
    return [p for p in (store.db, Path(str(store.db) + "-wal"), Path(str(store.db) + "-shm"))
            if p.exists()]


def apply(store: Store, importable: list[dict]) -> int:
    """Replay each task through the public API; restore the db on failure."""
    import shutil
    import tempfile

    backup_dir = Path(tempfile.mkdtemp(prefix="legacy-import-"))
    saved = []
    for path in _snapshot(store):
        target = backup_dir / path.name
        shutil.copy2(path, target)
        saved.append((path, target))
    try:
        for item in importable:
            # create() already lands the task in Taizi, so replay from there on.
            store.create(item["id"], item["title"][:200])
            current = store.get(item["id"])["state"]
            while current != item["state"]:
                next_state = FLOW[FLOW.index(current) + 1]
                actor = owner_of(current)
                command = STEP_COMMAND.get((current, next_state), "state")
                store.move(item["id"], actor, next_state, "legacy import", command=command)
                current = next_state
    except Exception:
        for original, backup in saved:
            shutil.copy2(backup, original)
        shutil.rmtree(backup_dir, ignore_errors=True)
        raise
    shutil.rmtree(backup_dir, ignore_errors=True)
    return len(importable)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--from", dest="source", type=Path, required=True,
                        help="legacy data directory or tasks_source.json")
    parser.add_argument("--home", type=Path, required=True, help="fusion instance home")
    parser.add_argument("--board-key-file", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    try:
        records = load_legacy(args.source)
    except LegacyError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    importable, skipped = plan(records)
    print(f"records={len(records)} importable={len(importable)} skipped={len(skipped)}")
    for row in skipped[:20]:
        print(f"  skip {row.get('id', '(index %d)' % row['index'])}: {row['reason']}")
    if len(skipped) > 20:
        print(f"  ... {len(skipped) - 20} more")
    if args.dry_run:
        print("dry run: nothing written")
        return 0
    if not importable:
        print("nothing to import")
        return 0

    archived = [row for row in skipped if row.get("record") is not None]
    if archived:
        archive_path = Path(args.home) / "legacy-archive.json"
        archive_path.parent.mkdir(parents=True, exist_ok=True)
        archive_path.write_text(json.dumps(archived, ensure_ascii=False, indent=2),
                                encoding="utf-8")
        print(f"archived {len(archived)} terminal records to {archive_path}")

    store = Store(args.home, args.board_key_file) if args.board_key_file else Store(args.home)
    try:
        count = apply(store, importable)
    except (PolicyError, ValueError) as exc:
        print(f"aborted, instance unchanged: {exc}", file=sys.stderr)
        return 1
    print(f"imported {count} tasks into {args.home}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
