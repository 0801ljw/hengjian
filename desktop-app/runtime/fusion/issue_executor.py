"""Execute one claimed Issue through an agent binding and materialize its output.

The executor is deliberately separate from the queue: the queue owns leases and
state, this module owns one bounded model round and safe artifact writing.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path

from .ai import DecisionClient
from .agent_channels import validate_binding
from .core import PolicyError
from .role_binding import DEFAULT_PRESET, ROLES, load
from .pricing import estimate
from .agent_workspace import file_list, new_run, recent_issues, workspace

MAX_ARTIFACT_BYTES = 262144
MAX_ARTIFACTS = 8


def resolve_binding(issue, instance):
    assignee = issue.get("assignee")
    model = issue.get("model") or None
    if assignee in ROLES:
        binding = (load(instance).get("bindings") or {}).get(assignee) or DEFAULT_PRESET.get(assignee)
        if not binding:
            raise PolicyError(f"no binding for role: {assignee}")
        agent = binding.get("agent")
        model = binding.get("model") or model
    else:
        agent = assignee
    if not isinstance(agent, str) or not agent:
        raise PolicyError("issue has no usable assignee")
    # Validate endpoint/model shape now; credentials are checked by the actual
    # run so setup remains possible before a key is installed.
    channel = validate_binding(agent, model, instance=instance)
    return {"agent": agent, "model": channel[3] or model or ""}


def _safe_artifact_path(root: Path, relative: str) -> Path:
    if not isinstance(relative, str) or not relative or len(relative) > 500:
        raise PolicyError("artifact path is required")
    if relative.startswith("/") or "\\" in relative or "\x00" in relative:
        raise PolicyError("artifact path must be relative")
    parts = Path(relative).parts
    if any(part in ("", ".", "..") for part in parts):
        raise PolicyError("artifact path traversal refused")
    target = (root / relative).resolve()
    root_resolved = root.resolve()
    if root_resolved != target and root_resolved not in target.parents:
        raise PolicyError("artifact escapes workspace")
    return target


def materialize_artifacts(run_dir: Path, artifacts) -> list[dict]:
    if not isinstance(artifacts, list):
        raise PolicyError("artifacts must be a list")
    if len(artifacts) > MAX_ARTIFACTS:
        raise PolicyError("too many artifacts")
    root = run_dir / "artifacts"
    root.mkdir(parents=True, exist_ok=True)
    written = []
    for item in artifacts:
        if not isinstance(item, dict) or set(item) != {"path", "content"}:
            raise PolicyError("invalid artifact shape")
        path = _safe_artifact_path(root, item["path"])
        content = item["content"]
        if not isinstance(content, str):
            raise PolicyError("artifact content must be text")
        data = content.encode("utf-8")
        if len(data) > MAX_ARTIFACT_BYTES:
            raise PolicyError("artifact exceeds 262144 bytes")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        written.append({"path": str(path.relative_to(root)), "bytes": len(data),
                        "sha256": hashlib.sha256(data).hexdigest()})
    return written


def verify_checks(run_dir: Path, checks) -> list[dict]:
    root = run_dir / "artifacts"
    results = []
    for check in checks:
        path = _safe_artifact_path(root, check.get("path", ""))
        exists = path.is_file()
        result = {"type": check["type"], "path": check["path"], "ok": False}
        if not exists:
            result["detail"] = "file not found"
        elif check["type"] == "file_exists":
            result["ok"] = True
        elif check["type"] == "sha256":
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            result["detail"] = digest
            result["ok"] = digest == check.get("sha256")
        elif check["type"] == "contains":
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                result["detail"] = "not utf-8 text"
            else:
                result["ok"] = str(check.get("contains", "")) in text
        results.append(result)
    return results


def usage_with_cost(instance, summary, fallback_model):
    steps = summary.get("steps") or []
    last = steps[-1] if steps else {}
    tokens = summary.get("tokens") or []
    prompt = sum(item.get("prompt_tokens") for item in tokens
                 if isinstance(item, dict) and isinstance(item.get("prompt_tokens"), int))
    completion = sum(item.get("completion_tokens") for item in tokens
                     if isinstance(item, dict) and isinstance(item.get("completion_tokens"), int))
    model = last.get("returned_model") or last.get("model") or fallback_model
    priced = estimate(instance, model, prompt, completion) if tokens else {
        "pricing": "unpriced", "cost_usd": None, "model": model}
    return {
        "total_tokens": summary.get("total_tokens"),
        "prompt_tokens": prompt if tokens else None,
        "completion_tokens": completion if tokens else None,
        "model": model,
        **priced,
    }


def execute_issue(issue, instance, *, transport=None) -> dict:
    """Run one bounded model round. The caller owns the claim and must settle it."""
    instance = Path(instance)
    budget = int(issue.get("max_tokens") or 100000)
    reserved = issue.get("budget_reserved")
    remaining = issue.get("budget_remaining")
    if isinstance(reserved, int):
        budget = min(budget, reserved)
    elif isinstance(remaining, int):
        budget = min(budget, remaining)
    if budget < 1000:
        return {"status": "blocked", "blocker": "root token budget exhausted",
                "usage": None, "retryable": False}
    binding = resolve_binding(issue, instance)
    agent_id = binding.get("agent")
    max_cost = issue.get("max_cost_usd")
    if max_cost is not None:
        priced = estimate(instance, binding.get("model") or "", 0, 0)
        if priced.get("pricing") != "configured":
            return {"status": "blocked", "blocker": "cost cap requires configured pricing",
                    "usage": None, "retryable": False}
    run_dir = new_run(instance, agent_id, issue["id"])
    agent_space = workspace(instance, agent_id)
    client = DecisionClient(run_dir / "round", transport=transport,
                            max_calls=int(issue.get("max_calls") or 4), instance=instance,
                            bindings={"issue_execute": binding})
    context = {
        "issue": {
            "id": issue["id"], "title": issue["title"], "body": issue["body"],
            "acceptance": issue.get("acceptance") or [],
            "parent_id": issue.get("parent_id"),
            "children": [{"id": child["id"], "title": child.get("title"),
                          "state": child.get("state"), "result": child.get("result")}
                         for child in issue.get("children") or []],
            "recent_issues": recent_issues(instance, issue.get("assignee"), issue["id"]),
        },
        "workspace": str(agent_space.resolve()),
        "run_workspace": str(run_dir.resolve()),
        "workspace_files": file_list(agent_space),
        "rules": {
            "no_network": True, "no_commands": True,
            "artifacts": "relative paths only; max 8 files; max 262144 bytes each",
            "token_budget": budget,
        },
    }
    value = client.decide("issue_execute", context)
    usage = usage_with_cost(instance, client.summary(), binding.get("model") or "")
    tokens = usage.get("total_tokens")
    summary = value["summary"]
    if isinstance(tokens, int) and tokens > budget:
        return {"status": "blocked", "blocker": f"token budget exceeded: {tokens}>{budget}",
                "run_dir": str(run_dir), "summary": summary, "usage": usage,
                "retryable": False}
    if max_cost is not None:
        cost = usage.get("cost_usd")
        if cost is None or cost > max_cost:
            return {"status": "blocked",
                    "blocker": "cost budget exceeded" if cost is not None else "cost is unpriced",
                    "run_dir": str(run_dir), "summary": summary, "usage": usage,
                    "retryable": False}
    if value["status"] == "delegate":
        return {"status": "delegate", "summary": summary,
                "subtasks": value.get("subtasks") or [], "run_dir": str(run_dir),
                "workspace": str(agent_space), "usage": usage}
    if value["status"] == "blocked":
        return {"status": "blocked", "blocker": value["blocker"] or "agent reported blocked",
                "run_dir": str(run_dir), "summary": summary, "retryable": False,
                "usage": usage}
    artifacts = materialize_artifacts(run_dir, value.get("artifacts") or [])
    checked = verify_checks(run_dir, issue.get("checks") or [])
    failed = [item for item in checked if not item["ok"]]
    if failed:
        detail = "; ".join(f'{item["type"]}:{item["path"]}' for item in failed)
        return {"status": "blocked", "blocker": f"acceptance checks failed: {detail}",
                "run_dir": str(run_dir), "summary": summary, "checks": checked,
                "retryable": True, "usage": usage}
    return {"status": "done", "summary": summary, "artifacts": artifacts,
            "checks": checked, "run_dir": str(run_dir), "workspace": str(agent_space),
            "usage": usage}
