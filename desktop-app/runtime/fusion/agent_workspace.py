"""Per-agent workspace and recent-run context.

CLI/HTTP channels are invoked per Issue; this is not a process supervisor. It
does provide a stable workspace, a per-agent run directory, and a bounded
recent-Issue summary so an agent does not start every task from zero.
"""
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from uuid import uuid4

SAFE = re.compile(r"[A-Za-z0-9_.-]{1,100}")


def _check(agent):
    if not isinstance(agent, str) or not SAFE.fullmatch(agent):
        raise ValueError("invalid agent id")


def agent_root(instance, agent) -> Path:
    _check(agent)
    root = Path(instance) / "agents" / agent
    root.mkdir(parents=True, exist_ok=True)
    root.chmod(0o700)
    return root


def workspace(instance, agent) -> Path:
    path = agent_root(instance, agent) / "workspace"
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)
    return path


def new_run(instance, agent, issue_id) -> Path:
    _check(agent)
    runs = agent_root(instance, agent) / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    runs.chmod(0o700)
    path = runs / f'{issue_id}-{uuid4().hex[:8]}'
    path.mkdir(parents=True, exist_ok=False)
    return path


def recent_issues(instance, assignee, exclude_id=None, limit=5):
    path = Path(instance) / "fusion.sqlite3"
    if not path.exists():
        return []
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
    except sqlite3.Error:
        return []
    try:
        rows = conn.execute("""
            SELECT id,title,state,result FROM issues
            WHERE assignee=? AND id<>? ORDER BY updated_at DESC LIMIT ?""",
            (assignee, exclude_id or "", int(limit))).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()
    result = []
    for row in rows:
        item = {"id": row["id"], "title": row["title"], "state": row["state"]}
        try:
            body = json.loads(row["result"]) if row["result"] else {}
        except (TypeError, ValueError):
            body = {}
        if isinstance(body, dict):
            item["summary"] = body.get("summary")
            item["artifacts"] = [entry.get("path") for entry in body.get("artifacts") or []
                                 if isinstance(entry, dict)]
        result.append(item)
    return result


def file_list(workspace_path, limit=20):
    try:
        entries = sorted(Path(workspace_path).iterdir(), key=lambda item: item.name)
    except OSError:
        return []
    return [item.name for item in entries if item.is_file()][:int(limit)]


def inventory_fields(instance, agent, assignee=None):
    root = agent_root(instance, agent)
    space = workspace(instance, agent)
    recent = recent_issues(instance, assignee or agent, limit=1)
    return {
        "workspace": str(space),
        "workspace_files": file_list(space),
        "last_issue": recent[0] if recent else None,
        "agent_root": str(root),
    }


def runs(instance, agent, limit=20):
    """Read bounded per-run summaries; raw prompts/decisions stay on disk."""
    _check(agent)
    root = Path(instance) / "agents" / agent / "runs"
    if not root.is_dir():
        return []
    rows = []
    try:
        directories = sorted((item for item in root.iterdir() if item.is_dir()),
                             key=lambda item: item.stat().st_mtime, reverse=True)
    except OSError:
        return []
    for directory in directories[:int(limit)]:
        steps = []
        errors = []
        decisions = directory / "round" / "decisions.jsonl"
        try:
            lines = decisions.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            lines = []
        for line in lines[-50:]:
            try:
                record = json.loads(line)
            except (TypeError, ValueError):
                continue
            step = {"step": record.get("step"), "status": record.get("status"),
                    "model": record.get("returned_model") or record.get("model"),
                    "at": record.get("at")}
            if record.get("error"):
                step["error"] = str(record["error"])[:300]
                errors.append(step["error"])
            steps.append(step)
        artifacts = []
        try:
            artifacts = sorted(str(item.relative_to(directory / "artifacts"))
                               for item in (directory / "artifacts").rglob("*")
                               if item.is_file())[:32]
        except OSError:
            pass
        rows.append({
            "run": directory.name,
            "issue_id": directory.name.rsplit("-", 1)[0],
            "at": Path(directory).stat().st_mtime,
            "steps": steps,
            "errors": errors,
            "artifacts": artifacts,
            "workspace": str(workspace(instance, agent)),
        })
    return rows
