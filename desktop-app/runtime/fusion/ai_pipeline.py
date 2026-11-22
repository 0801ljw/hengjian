"""AI proposals -> deterministic validation -> existing authoritative mutation."""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime
from pathlib import Path

from .ai import runtime_context
from .ai_schema import check_order, check_plan
from .approvals import prepare_request
from .core import PolicyError, artifact
from .mutate import apply_mutation, canonical, payload_hash


MAX_SOURCE_BYTES = 64 * 1024
MAX_TOTAL_SOURCE_BYTES = 256 * 1024
MAX_SOURCES = 8


def load_sources(paths, *, roots):
    """Read operator-declared input files, bounded and audited.

    Only explicit paths under an allowed root are read; nothing is discovered.
    Returns (sources, rejected) where rejected entries explain why a path was
    refused instead of silently dropping it.
    """
    from pathlib import Path as _Path

    sources, rejected, total = [], [], 0
    for raw in list(paths)[:MAX_SOURCES]:
        path = _Path(raw)
        try:
            resolved = path.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            rejected.append({"path": str(raw), "reason": f"not readable: {exc}"})
            continue
        if not resolved.is_file():
            rejected.append({"path": str(raw), "reason": "not a regular file"})
            continue
        allowed = any(resolved.is_relative_to(_Path(root).resolve()) for root in roots)
        if not allowed:
            rejected.append({"path": str(raw), "reason": "outside allowed roots"})
            continue
        size = resolved.stat().st_size
        if size > MAX_SOURCE_BYTES:
            rejected.append({"path": str(raw), "reason": f"larger than {MAX_SOURCE_BYTES} bytes"})
            continue
        if total + size > MAX_TOTAL_SOURCE_BYTES:
            rejected.append({"path": str(raw), "reason": "total source budget exceeded"})
            continue
        try:
            text = resolved.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            rejected.append({"path": str(raw), "reason": "not UTF-8 text"})
            continue
        digest = hashlib.sha256(resolved.read_bytes()).hexdigest()
        sources.append({"path": str(resolved), "sha256": digest, "bytes": size, "text": text})
        total += size
    return sources, rejected


class Pipeline:
    def __init__(self, store, client, task_id):
        self.store, self.client, self.task_id = store, client, task_id
        self.session = uuid.uuid4().hex
        self.task = None
        # Frozen once for the whole run: every role and every retry must see the
        # same clock, otherwise a plan quoting "now" is stale on arrival at review.
        self.now = datetime.now().astimezone()

    def decide(self, role, context, *, workspace=None):
        if role in {"zhongshu", "menxia", "shangshu", "gongbu"}:
            context = {**context, "runtime": runtime_context(
                workspace if workspace is not None else self.store.home, role, now=self.now)}
        return self.client.decide(role, context)

    def mutate(self, actor, command, target, reason, *, payload=None, recipient=None):
        old_payload = self.task["payload"] if self.task else payload
        self.task = apply_mutation(
            self.store, self.task_id, actor, command, target, reason,
            payload=payload, recipient=recipient, payload_sha256=payload_hash(old_payload),
            expected_revision=self.task["revision"] if self.task else 0,
            run_id=f"ai:{self.session}:{actor}",
            fencing_token=self.task.get("fencing_token", 0) if self.task else 0)
        return self.task

    def run(self, request, *, allow_unpriced=False, sources=None):
        if not isinstance(request, str) or not request.strip() or len(request) > 12000:
            raise PolicyError("request must be 1..12000 characters")
        route = self.client.decide("taizi", {"request": request})
        if route["kind"] == "chat":
            return {"status": "chat", "route": route, "task_created": False}
        self.mutate("taizi", "create", "Taizi", route["reason"],
                    payload={"title": route["title"], "requirement": route["requirement"],
                             "original_request": request, "route": route})
        self.mutate("taizi", "state", "Zhongshu", route["reason"], recipient="zhongshu")
        plan = self.review_loop(route["requirement"])
        if plan is None:
            return {"status": "rejected", "task": self.task, "replans": 2}
        dispatch = self.decide("shangshu", {"plan": plan})
        check_order(dispatch["order"], plan)
        self.mutate("shangshu", "payload", "Assigned", "AI dispatch proposal",
                    payload={**self.task["payload"], "dispatch": dispatch})
        self.mutate("shangshu", "state", "Doing", dispatch["reason"], recipient="gongbu")
        return self.execute(plan, dispatch, allow_unpriced, sources=sources)

    def review_loop(self, requirement):
        feedback = None
        for revision in range(3):  # initial plan + at most TWO replans
            plan = self.decide("zhongshu", {"requirement": requirement,
                                                   "feedback": feedback, "revision": revision})
            check_plan(plan)
            self.mutate("zhongshu", "payload", "Zhongshu", "AI plan",
                        payload={**self.task["payload"], "plan": plan, "plan_revision": revision})
            self.mutate("zhongshu", "state", "Menxia", "submit AI plan", recipient="menxia")
            review = self.decide("menxia", {"requirement": requirement, "plan": plan})
            # Keep the submitting Zhongshu actor/revision intact until review.
            # The mutation's immutable review_decision and AI audit retain the
            # verdict; a prior Menxia payload write would look like self-review.
            reason = "; ".join(review["reasons"])
            if review["verdict"] == "approve":
                self.mutate("menxia", "state", "Assigned", reason, recipient="shangshu")
                return plan
            self.mutate("menxia", "state", "Zhongshu", reason, recipient="zhongshu")
            feedback = review
        self.mutate("zhongshu", "state", "Cancelled", "封驳两次重拟后仍未通过，终止")
        return None

    def _block_after_failure(self, error: BaseException) -> None:
        """Surface a failed execution as Blocked instead of leaving it in Doing.

        Narrow by construction: only from Doing, as the state owner gongbu, and
        through the single mutation service. The block is best-effort - a run
        failure is already recorded in the run row and the worker log - so a
        concurrent state change or a lost fence must not mask the original
        exception.
        """
        try:
            task = self.store.get(self.task_id)
            if task.get("state") != "Doing":
                return
            apply_mutation(
                self.store, self.task_id, "gongbu", "block", "Blocked",
                f"execution failed: {type(error).__name__}",
                payload_sha256=payload_hash(task.get("payload", {})),
                expected_revision=task["revision"],
                run_id=f"ai:{self.session}:gongbu:failed",
                fencing_token=task.get("fencing_token", 0))
        except Exception:
            pass

    def execute(self, plan, dispatch, allow_unpriced, *, sources=None):
        # No worker model is called and no workspace output is written until BOTH
        # budget admission and a fenced lease succeed in the single mutation service.
        run_id = self.store.checkout(self.task_id, allow_unpriced)
        self.task = self.store.get(self.task_id)
        workspace = self.store.home / "runs" / run_id
        files = []
        audit_start = len(self.client.summary()["steps"])
        try:
            workspace.mkdir(parents=True, exist_ok=False)
            tasks = {task["id"]: task for task in plan["subtasks"]}
            prior = {}
            for key in dispatch["order"]:
                target_dir = workspace if len(dispatch["order"]) == 1 else workspace / key
                output = self.decide("gongbu", {
                    "requirement": self.task["payload"]["requirement"],
                    "subtask": tasks[key], "prior_outputs": prior,
                    "sources": sources or []}, workspace=target_dir)
                # A single-subtask plan writes at the workspace root (the simple
                # case stays predictable); multi-subtask plans get one directory
                # per subtask so two workers cannot collide on the same name.
                produced = write_files(target_dir, output["files"])
                for item in produced:
                    item["subtask"] = key
                    item["primary"] = key == dispatch["order"][-1]
                files.extend(produced)
                prior[key] = output
            manifest = workspace / "manifest.json"
            manifest.write_text(canonical({
                "files": files, "reports": prior,
                "primary_subtask": dispatch["order"][-1],
                "sources": [{k: v for k, v in src.items() if k != "text"}
                            for src in (sources or [])]}))
            worker_usage = [record["tokens"] for record in self.client.summary()["steps"][audit_start:]
                            if record["step"] == "gongbu"]
            result = {"status": "succeeded", "artifact": artifact(manifest),
                      "usage": {"worker_calls": worker_usage}, "cost_usd": None}
            self.store.finish(run_id, result)
        except BaseException as exc:
            self.store.finish(run_id, {"status": "failed", "cost_usd": None})
            self._block_after_failure(exc)
            raise
        self.task = self.store.get(self.task_id)
        self.mutate("gongbu", "done", "Review", "AI files materialized; board closure still required",
                    recipient="shangshu")
        frozen = prepare_request(
            self.store, self.task_id, "shangshu", "Done", "验收模型生成文件，申请完结",
            expected_revision=self.task["revision"], payload_sha256=payload_hash(self.task["payload"]),
            run_id=f"ai:{self.session}:shangshu")
        (workspace / "board-request.json").write_text(canonical(frozen))
        return {"status": "awaiting_board", "state": "Review", "run_id": run_id,
                "workspace": str(workspace), "files": files, "board_request": frozen,
                "artifact": result["artifact"]}


def write_files(workspace, files):
    """Text-only local adapter; never interpret a model's paths or contents as code."""
    candidates = []
    for item in files:
        relative = Path(item["path"])
        if (relative.is_absolute() or not relative.parts or len(relative.parts) > 5
                or any(not re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9_.-]{0,79}", part)
                       or part in {".", ".."} for part in relative.parts)
                or relative.parts[0] in {"manifest.json", "board-request.json"}):
            raise PolicyError("unsafe or reserved output path")
        path = workspace / relative
        if not path.resolve().is_relative_to(workspace.resolve()) or path.exists() or path.is_symlink():
            raise PolicyError("output path escapes workspace or already exists")
        candidates.append((path, item["content"]))
    if len({path for path, _ in candidates}) != len(candidates):
        raise PolicyError("duplicate output paths")
    artifacts = []
    for path, content in candidates:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x") as output:
            output.write(content)
        artifacts.append(artifact(path))
    return artifacts
