"""Pure UI projections. No filesystem, server, model calls or ledger mutations.

Adapters may pass an explicit path -> authenticated same-origin download URL map.
Paths in the ledger are metadata, never automatically exposed as HTTP URLs.
"""
from __future__ import annotations

from copy import deepcopy
from urllib.parse import urlsplit

from .ai_view import run_view

ACTORS = {
    "taizi": "太子判定", "zhongshu": "中书计划", "menxia": "门下裁决",
    "shangshu": "尚书派发", "gongbu": "工部产物",
}


def render_timeline(view: dict) -> list[dict]:
    """Project run_view steps, distinguishing review rounds from model retries."""
    timeline = []
    review_attempt = 0
    rejected = False
    for step in view.get("steps", []):
        actor = step.get("actor")
        decision = step.get("decision") or {}
        valid = step.get("status") in {"valid", "ledger"}
        verdict = decision.get("verdict") if valid else None
        is_review = actor == "menxia" and verdict in {"reject", "approve"}
        if is_review:
            review_attempt += 1
        revised = is_review and rejected
        if is_review and verdict == "reject":
            rejected = True
        timeline.append({
            "sequence": len(timeline) + 1,
            "actor": actor, "label": ACTORS.get(actor, actor or "未知步骤"),
            "timestamp": step.get("timestamp"), "status": step.get("status"),
            "model_attempt": step.get("attempt", 1),
            "plan_revision": step.get("plan_revision"),
            "review_attempt": review_attempt if is_review else None,
            "verdict": verdict if is_review else None,
            "revised": revised, "passed_after_revision": revised and verdict == "approve",
            "reasons": deepcopy(step.get("reasons", [])),
            "required_changes": deepcopy(step.get("required_changes", [])),
            "decision": deepcopy(decision), "error": step.get("error"),
        })
    return timeline


def _download_url(value: object) -> str | None:
    if not isinstance(value, str) or not value.startswith("/"):
        return None
    if "\\" in value or any(ord(char) <= 32 or ord(char) == 127 for char in value):
        return None
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    return value if not parsed.scheme and not parsed.netloc and not value.startswith("//") else None


def render_artifacts(view: dict, download_urls: dict | None = None) -> list[dict]:
    """List metadata; only explicit adapter-supplied same-origin URLs are links."""
    urls = download_urls or {}
    artifacts = []
    for item in view.get("artifacts", []):
        if not isinstance(item, dict):
            continue
        path = str(item.get("path", ""))
        artifacts.append({
            "name": path.replace("\\", "/").rsplit("/", 1)[-1] or "未命名产物",
            "path": path, "bytes": item.get("bytes"), "sha256": item.get("sha256"),
            "download_url": _download_url(urls.get(path)),
        })
    return artifacts


def render_run(view: dict, download_urls: dict | None = None) -> dict:
    """Convert an existing API run_view to the static page's display structure."""
    return {
        "id": view.get("id"), "title": view.get("title", view.get("id")),
        "status": view.get("status", "incomplete"), "state": view.get("state"),
        "source": view.get("source", "ledger"),
        "timestamp": view.get("timestamp"), "timeline": render_timeline(view),
        "artifacts": render_artifacts(view, download_urls),
        "runs": deepcopy(view.get("runs", [])),
    }


def render_ledger(snapshot: dict, task_id: str, records: list | None = None,
                  download_urls: dict | None = None) -> dict:
    """Project a supplied snapshot and optional audit, without reading a Store."""
    return render_run(run_view(snapshot, records or [], task_id), download_urls)


def render_tasks(snapshot: dict) -> list[dict]:
    """Summarize tasks and their worker runs without sharing mutable references."""
    return [{
        "id": task["id"],
        "title": task.get("title", task.get("payload", {}).get("title", task["id"])),
        "state": task.get("state"),
        "runs": deepcopy([run for run in snapshot.get("runs", [])
                          if run.get("task_id") == task["id"]]),
    } for task in snapshot.get("tasks", [])]
