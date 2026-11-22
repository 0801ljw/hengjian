"""启动期一致性回收：把"进程已死但状态还挂着"的东西收回终态。

背景（1.1-1）：主机断电/进程被杀后，队列里的提交会永远停在 `running`，
导致 UI 显示"运行中"而实际没有任何进程在跑，且消费端只处理 `queued`，
不会自愈。租约层已有 `leases.recover`（标记 run 为 orphaned），
但**队列项**没有对应回收，本模块补齐这一层。

安全边界：
- 只在"能拿到 consumer.lock"时回收 —— 拿不到说明还有活着的消费进程，绝不能动。
- 只动 `running` 状态；`queued`/终态一律不碰。
- 幂等：回收后再跑一次不会再产生变更。
"""
from __future__ import annotations

import fcntl
import json
from pathlib import Path

from .core import now

ORPHAN_ERROR = {
    "error_type": "orphaned_after_restart",
    "message": "消费进程在执行中退出；已回收为终态，需人工确认是否重跑",
}


def _queue_dir(store) -> Path:
    return store.home / "requests"


def _reclaim_queue(store) -> dict:
    queue = _queue_dir(store)
    result = {"reclaimed": [], "skipped": "", "checked": 0}
    if not queue.is_dir():
        result["skipped"] = "no_queue"
        return result
    lock_path = queue / "consumer.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            result["skipped"] = "consumer_online"
            return result
        try:
            from .core import atomic_json_write
            for path in sorted(queue.glob("AI-*.json")):
                try:
                    item = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                result["checked"] += 1
                if item.get("status") != "running":
                    continue
                item.update(status="error", ended_at=now(), result={"error": dict(ORPHAN_ERROR)})
                atomic_json_write(path, item)
                result["reclaimed"].append(item.get("id"))
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
    return result


def reconcile(store) -> dict:
    """回收租约/预算残留 + 队列里挂着 running 的提交（幂等）。

    返回结构用于日志与测试：
      {"leases": <budget.recover 的返回值或 None>,
       "queue": {"reclaimed": [...], "skipped": "...", "checked": N}}
    """
    from .budget import recover as recover_budget

    recover_budget(store)
    return {"leases": "recovered", "queue": _reclaim_queue(store)}
