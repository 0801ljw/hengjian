"""1.1-1 行为测试：断电/崩溃后不留永久 running。

覆盖：
1. 队列里挂着 running 的提交 → 启动回收为 error（错误码 orphaned_after_restart）
2. 有活的消费进程（持有 consumer.lock）→ 绝不回收
3. 回收是幂等的（第二次跑没有变更）
4. 死进程持有的租约 → run 标 orphaned、lease owner 清空、fencing 递增
5. 活进程持有的租约 → 不动
"""
import fcntl
import json
import os
import sqlite3
import time

import pytest

from fusion.core import Store, now
from fusion.leases import TTL, acquire, read
from fusion.recovery import ORPHAN_ERROR, reconcile


TASK = "AI-orphan-test"
DEAD_PID = 4_000_000  # 几乎不可能存在的 PID


def make_store(tmp_path):
    return Store(tmp_path / "home")


def write_submission(store, key, status="running"):
    queue = store.home / "requests"
    queue.mkdir(parents=True, exist_ok=True)
    payload = {"id": key, "status": status, "who": "probe", "request": "x", "time": now()}
    (queue / (key + ".json")).write_text(json.dumps(payload), encoding="utf-8")
    return payload


def read_submission(store, key):
    return json.loads((store.home / "requests" / (key + ".json")).read_text(encoding="utf-8"))


def insert_task_and_run(store, task_id, run_id, status="running"):
    with store.transaction() as conn:
        conn.execute("INSERT OR REPLACE INTO tasks VALUES(?,?)", (task_id, json.dumps({"id": task_id})))
        conn.execute("INSERT OR REPLACE INTO runs VALUES(?,?,?,?)",
                     (run_id, task_id, status, json.dumps({"id": run_id, "status": status})))


def insert_dead_lease(store, task_id, run_id):
    with store.transaction() as conn:
        now_ts = time.time()
        conn.execute("INSERT OR REPLACE INTO leases VALUES(?,?,?,?,?,?,?,?)",
                     (task_id, run_id, 7, now_ts, now_ts + TTL, now_ts, DEAD_PID, "dead-identity"))


def test_running_submission_is_reclaimed(tmp_path):
    store = make_store(tmp_path)
    write_submission(store, TASK, "running")

    outcome = reconcile(store)
    assert TASK in outcome["queue"]["reclaimed"]

    item = read_submission(store, TASK)
    assert item["status"] == "error"
    assert item["result"]["error"]["error_type"] == ORPHAN_ERROR["error_type"]


def test_live_consumer_is_never_disturbed(tmp_path):
    store = make_store(tmp_path)
    write_submission(store, TASK, "running")

    queue = store.home / "requests"
    with (queue / "consumer.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        outcome = reconcile(store)
    assert outcome["queue"]["skipped"] == "consumer_online"
    assert read_submission(store, TASK)["status"] == "running"


def test_reconcile_is_idempotent(tmp_path):
    store = make_store(tmp_path)
    write_submission(store, TASK, "running")

    first = reconcile(store)
    assert first["queue"]["reclaimed"] == [TASK]
    second = reconcile(store)
    assert second["queue"]["reclaimed"] == []


def test_dead_lease_revokes_run_and_clears_owner(tmp_path):
    store = make_store(tmp_path)
    insert_task_and_run(store, TASK, "ai:probe:gongbu")
    insert_dead_lease(store, TASK, "ai:probe:gongbu")

    reconcile(store)

    with store.connect() as conn:
        lease = read(conn, TASK)
        run_status = conn.execute("SELECT status FROM runs WHERE id=?", ("ai:probe:gongbu",)).fetchone()[0]
    assert lease["owner_id"] is None
    assert lease["owner_pid"] is None
    assert lease["fencing_token"] == 8          # 撤销时栅栏前进一步
    assert run_status == "orphaned"


def test_live_lease_is_kept(tmp_path):
    store = make_store(tmp_path)
    insert_task_and_run(store, "AI-live", "ai:live:gongbu")
    with store.transaction() as conn:
        acquire(conn, "AI-live", "ai:live:gongbu")
        token_before = read(conn, "AI-live")["fencing_token"]

    reconcile(store)

    with store.connect() as conn:
        lease = read(conn, "AI-live")
    assert lease["owner_id"] == "ai:live:gongbu"
    assert lease["fencing_token"] == token_before
