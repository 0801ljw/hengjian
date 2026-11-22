"""Read-only stuck-task audit with narrow, audited recovery actions.

`fusion stuck` lists tasks that are not terminal and explains *why* they are not
moving: the board gate (Review/PendingConfirm), a stalled AI stage
(Menxia/Assigned/Doing), or a Blocked task that needs a human decision.

The default mode is read-only. `--apply-block` performs only transitions the
frozen upstream policy already allows for the current state owner
(Assigned/Next/Doing -> Blocked) through `apply_mutation`, so every change is
audited like any other write. `Doing -> Blocked` uses the `block` command that
the frozen policy already grants gongbu; a failed run also marks its own task
Blocked automatically. The tool never signs an approval, never bypasses the
board gate, and never re-submits work by itself: stalled AI work is reported
with the exact `fusion run` command a human may choose to pay for and re-run.
"""
from __future__ import annotations

import argparse
import json
import shlex
import sys
from datetime import datetime, timezone
from pathlib import Path

from .core import PolicyError, Store, company_home, now as core_now
from .mutate import apply_mutation, payload_hash

TERMINAL = {"Done", "Cancelled"}
BOARD_WAIT = {"Review"}
# state -> (owner actor, command understood by the frozen policy)
SAFE_BLOCK = {
    "Assigned": ("shangshu", "state"),
    "Next": ("shangshu", "state"),
    "Doing": ("gongbu", "block"),
}
DEFAULT_IDLE_HOURS = 2.0
DEFAULT_REASON = "巡检：任务长时间无进展，标记 Blocked 等待人工处置"


def _parse_ts(value: str | None) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _latest_runs(snapshot: dict) -> dict[str, dict]:
    """Pick the most recent run per task without trusting list order."""
    latest: dict[str, dict] = {}
    for run in snapshot.get("runs", []):
        task_id = run.get("task_id")
        started = _parse_ts(run.get("started_at"))
        current = latest.get(task_id)
        if task_id and (current is None or (started and started > current["started"])):
            latest[task_id] = {"status": run.get("status"), "started": started}
    return latest


def _idle_hours(task: dict, latest_run: dict | None, now: datetime) -> float | None:
    stamps = [_parse_ts(task.get("updatedAt"))]
    if latest_run and latest_run.get("started"):
        stamps.append(latest_run["started"])
    known = [stamp for stamp in stamps if stamp is not None]
    if not known:
        return None
    return max(0.0, (now - max(known)).total_seconds() / 3600)


def classify(task: dict, latest_run: dict | None, *, now: datetime,
             idle_hours: float) -> dict | None:
    """Return a report for a non-terminal task, or None for terminal tasks."""
    state = task.get("state")
    if state in TERMINAL:
        return None
    idle = _idle_hours(task, latest_run, now)
    stalled = idle is None or idle >= idle_hours
    run_status = (latest_run or {}).get("status")
    if state in BOARD_WAIT:
        kind, action, stuck = "awaiting_board", "board_sign", True
    elif state == "PendingConfirm":
        kind, action, stuck = "pending_confirm", "confirm_or_reopen", True
    elif state == "Menxia":
        kind, action, stuck = "stalled_menxia", "return_to_zhongshu", stalled
    elif state == "Assigned":
        kind, action, stuck = "stalled_assigned", "block", stalled
    elif state == "Doing":
        if run_status == "failed":
            kind, action, stuck = "failed_run", "block", True
        else:
            kind, action, stuck = "stalled_doing", "block", stalled
    elif state == "Blocked":
        kind, action, stuck = "blocked", "needs_board", True
    else:
        kind, action, stuck = "pipeline_head", "cancel_or_requeue", stalled
    return {
        "task_id": task.get("id"),
        "title": task.get("title"),
        "state": state,
        "kind": kind,
        "action": action,
        "stuck": stuck,
        "idle_hours": None if idle is None else round(idle, 2),
        "run_status": run_status,
        "safe_to_block": state in SAFE_BLOCK,
    }


def requeue_command(task: dict, home: Path | str) -> str | None:
    """The exact documented re-submission command, or None when no text exists."""
    payload = task.get("payload") or {}
    request_text = payload.get("original_request") or payload.get("requirement") or task.get("title")
    if not isinstance(request_text, str) or not request_text.strip():
        return None
    return ("fusion run " + shlex.quote(request_text.strip())
            + f" --home {shlex.quote(str(home))} --allow-unpriced")


def scan(store: Store, *, idle_hours: float = DEFAULT_IDLE_HOURS,
         now: datetime | None = None) -> list[dict]:
    """Read-only audit of every task that has not reached a terminal state."""
    moment = now or _parse_ts(core_now()) or datetime.now(timezone.utc)
    snapshot = store.snapshot()
    latest = _latest_runs(snapshot)
    reports = []
    for task in snapshot.get("tasks", []):
        report = classify(task, latest.get(task.get("id")),
                          now=moment, idle_hours=idle_hours)
        if report is None:
            continue
        report["requeue"] = requeue_command(task, store.home)
        reports.append(report)
    reports.sort(key=lambda item: (-(item["idle_hours"] or 0.0), item["task_id"] or ""))
    return reports


def apply_block(store: Store, task_id: str, *, reason: str = DEFAULT_REASON,
                run_id: str | None = None) -> dict:
    """Move an Assigned/Doing task to Blocked as its owner; audited and narrow."""
    if not isinstance(reason, str) or not reason.strip():
        raise PolicyError("block reason is required")
    task = store.get(task_id)
    state = task.get("state")
    if state not in SAFE_BLOCK:
        raise PolicyError(f"refusing to block from state {state}; only "
                          f"{sorted(SAFE_BLOCK)} are safe. Menxia/Review need a human decision")
    actor, command = SAFE_BLOCK[state]
    return apply_mutation(
        store, task_id, actor, command, "Blocked", reason.strip(),
        payload_sha256=payload_hash(task.get("payload", {})),
        expected_revision=task["revision"],
        run_id=run_id or f"ops:stuck:{datetime.now(timezone.utc).date().isoformat()}",
        fencing_token=task.get("fencing_token", 0),
    )


def format_report(reports: list[dict]) -> str:
    if not reports:
        return "没有非终态任务。"
    lines = ["TASK                      STATE           IDLE(h)  KIND             ACTION"]
    for report in reports:
        idle = "-" if report["idle_hours"] is None else f"{report['idle_hours']:.1f}"
        lines.append("%-25s %-15s %-8s %-16s %s" % (
            (report["task_id"] or "")[:25], report["state"], idle,
            report["kind"], report["action"]))
        if report.get("requeue"):
            lines.append("    重投命令: " + report["requeue"])
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="fusion stuck",
        description="Audit non-terminal tasks; optionally mark Assigned/Next/Doing as Blocked.")
    parser.add_argument("--home", type=Path, required=True, help="instance root")
    parser.add_argument("--company", default="default")
    parser.add_argument("--board-key-file", type=Path, default=None)
    parser.add_argument("--hours", type=float, default=DEFAULT_IDLE_HOURS,
                        help="idle threshold in hours (default 2)")
    parser.add_argument("--apply-block", action="store_true",
                        help="mark stuck Assigned/Next/Doing tasks Blocked (audited)")
    parser.add_argument("--reason", default=DEFAULT_REASON)
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        store = Store(company_home(args.home, args.company), args.board_key_file)
        reports = scan(store, idle_hours=args.hours)
        applied = []
        if args.apply_block:
            for report in reports:
                if report["stuck"] and report["safe_to_block"]:
                    result = apply_block(store, report["task_id"],
                                         reason=args.reason, run_id=args.run_id)
                    applied.append({"task_id": report["task_id"],
                                    "state": (result or {}).get("state")})
        if args.json:
            print(json.dumps({"reports": reports, "applied": applied}, ensure_ascii=False, indent=2))
        else:
            print(format_report(reports))
            for item in applied:
                print(f"已标记 Blocked: {item['task_id']} -> {item['state']}")
    except PolicyError as error:
        print(f"fusion stuck: {error}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
