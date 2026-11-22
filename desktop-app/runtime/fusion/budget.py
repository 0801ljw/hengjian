"""Integer-cent budget ledger for the local, single-host controller."""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .core import Conflict, PolicyError, now


class BudgetError(PolicyError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def cents(value):
    if type(value) is not int or not 0 <= value <= 2**63 - 1:
        raise PolicyError("cents must be a nonnegative SQLite integer")
    return value


def month():
    return datetime.now(timezone.utc).strftime("%Y-%m")


def owner_token(pid):
    """Linux boot + process start time prevents PID-reuse false liveness."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        if stat[0] == "Z":
            return None
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip() + ":" + stat[19]
    except FileNotFoundError:
        return None


def initialize(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS budget_policy(
            singleton INTEGER PRIMARY KEY CHECK(singleton=1), revision INTEGER NOT NULL,
            body TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS budget_reservations(
            run_id TEXT PRIMARY KEY REFERENCES runs(id), agent TEXT NOT NULL,
            month TEXT NOT NULL, estimated_cents INTEGER, actual_cents INTEGER,
            status TEXT NOT NULL, unknown_spend INTEGER NOT NULL,
            owner_pid INTEGER NOT NULL, owner_token TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS budget_events(
            seq INTEGER PRIMARY KEY, at TEXT NOT NULL, run_id TEXT,
            kind TEXT NOT NULL, body TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS budget_overrides(
            id TEXT PRIMARY KEY, run_id TEXT UNIQUE NOT NULL, body TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS budget_denials(
            seq INTEGER PRIMARY KEY, at TEXT NOT NULL, run_id TEXT, agent TEXT,
            task_id TEXT, code TEXT NOT NULL, body TEXT NOT NULL);
    """)


def event(conn, kind, detail, run_id=None):
    conn.execute("INSERT INTO budget_events(at,run_id,kind,body) VALUES(?,?,?,?)",
                 (now(), run_id, kind, json.dumps(detail)))


def _canonical(value):
    """与 mutate.canonical 相同的规范序列化（这里不 import，避免模块耦合）。"""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False)


def record_denial(store, *, run_id, agent, task_id, code, detail=None):
    """暂存一条拒绝记录，等调用方事务结束后再落库。

    为什么不能立即写：admission 在 `BEGIN IMMEDIATE` 里，第二个连接会被写锁挡住；
    而事务回滚又会带走任何同事务的 event。所以先挂到 store 上，
    由 `flush_denials` 在事务外补写 —— 拒绝原因不会随回滚丢失。
    """
    pending = getattr(store, "_pending_denials", None)
    if pending is None:
        pending = []
        store._pending_denials = pending
    pending.append({"at": now(), "run_id": run_id, "agent": agent, "task_id": task_id,
                    "code": code, "body": _canonical(detail or {})})


def flush_denials(store):
    """把暂存的拒绝记录写入 budget_denials（事务外调用；返回写入条数）。"""
    pending = getattr(store, "_pending_denials", None)
    if not pending:
        return 0
    store._pending_denials = []
    try:
        conn = sqlite3.connect(str(store.db), timeout=5)
        try:
            conn.executemany(
                "INSERT INTO budget_denials(at,run_id,agent,task_id,code,body) VALUES(?,?,?,?,?,?)",
                [(r["at"], r["run_id"], r["agent"], r["task_id"], r["code"], r["body"])
                 for r in pending])
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error:
        # 记录失败不能改变拒绝本身的结果
        return 0
    return len(pending)


def policy(conn):
    row = conn.execute("SELECT body FROM budget_policy").fetchone()
    return json.loads(row[0])["document"] if row else {
        "company_cents": None, "agents": {}, "unpriced": "block"}


def import_policy(store, envelope):
    """Only offline Ed25519 board signatures may change persisted caps/policy."""
    from .approvals import _verify_signature, live
    from .mutate import canonical
    _verify_signature(store, envelope)
    doc = envelope["document"]
    required = {"id", "kind", "principal", "actor", "created_at", "expires_at",
                "revision", "window", "company_cents", "agents", "unpriced"}
    if (set(doc) != required or doc["kind"] != "budget_policy" or doc["principal"] != "board"
            or not isinstance(doc["actor"], str) or not doc["actor"].startswith("board:")
            or doc["window"] != "calendar_month_utc" or doc["unpriced"] not in {"block", "allow"}
            or not isinstance(doc["agents"], dict) or not doc["agents"]):
        raise PolicyError("invalid board budget policy")
    cents(doc["company_cents"])
    cents(doc["revision"])
    for agent, limit in doc["agents"].items():
        if agent != "gongbu":
            raise PolicyError("unsupported runtime agent")
        cents(limit)
    live(doc)
    with store.transaction() as conn:
        prior = conn.execute("SELECT revision,body FROM budget_policy").fetchone()
        if prior and prior[1] == canonical(envelope):
            return doc
        if prior and doc["revision"] <= prior[0]:
            raise Conflict("stale budget policy revision")
        conn.execute("INSERT OR REPLACE INTO budget_policy VALUES(1,?,?)",
                     (doc["revision"], canonical(envelope)))
        event(conn, "policy_changed", envelope)
    return doc


def status(conn, agent="gongbu", window=None):
    window = window or month()
    rows = conn.execute("SELECT agent,estimated_cents,actual_cents,status,unknown_spend "
                        "FROM budget_reservations WHERE month=?", (window,)).fetchall()
    def totals(selected):
        return {"known_spend_cents": sum(r[2] for r in selected if r[2] is not None),
                "reserved_cents": sum(r[1] for r in selected if r[3] == "reserved" and r[1] is not None),
                "unknown_spend": any(bool(r[4]) for r in selected)}
    company = totals(rows)
    return {"window": "calendar_month_utc", "month": window, "company": company,
            "agent": totals([r for r in rows if r[0] == agent]),
            "unknown_spend": company["unknown_spend"]}


def override_caps(store, conn, envelope, run_id, task_id, estimate, window):
    from .approvals import _verify_signature, live
    from .mutate import canonical
    _verify_signature(store, envelope)
    doc = envelope["document"]
    if (set(doc) != {"id", "kind", "principal", "actor", "created_at", "expires_at",
                    "run_id", "task_id", "month", "estimated_cents", "company_extra_cents",
                    "agent_extra_cents"}
            or doc["kind"] != "budget_override" or doc["principal"] != "board"
            or not isinstance(doc["actor"], str) or not doc["actor"].startswith("board:")
            or doc["run_id"] != run_id or doc["task_id"] != task_id
            or doc["month"] != window or doc["estimated_cents"] != estimate):
        raise PolicyError("invalid budget override binding")
    live(doc)
    extras = (cents(doc["company_extra_cents"]), cents(doc["agent_extra_cents"]))
    try:
        conn.execute("INSERT INTO budget_overrides VALUES(?,?,?)", (doc["id"], run_id, canonical(envelope)))
    except sqlite3.IntegrityError as exc:
        raise Conflict("budget override already consumed") from exc
    event(conn, "override", envelope, run_id)
    return extras


def _unpriced_cap(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return max(0, value)


def unpriced_caps() -> tuple[int, int]:
    """Monthly unpriced dispatch caps: (company, per-agent).

    Env-configurable so a deployment can tighten or relax without a schema
    change; the defaults prevent --allow-unpriced from becoming unlimited.
    """
    return (_unpriced_cap("DUTANG_UNPRICED_MONTHLY_MAX", 20),
            _unpriced_cap("DUTANG_UNPRICED_AGENT_MAX", 10))


def reserve(store, conn, run_id, task_id, agent, request, *, fencing_token):
    from .leases import check
    check(conn, task_id, fencing_token, active=True, owner_id=run_id)
    window = month()  # Freeze the accounting month once, even across a UTC rollover.
    provider = request.get("provider", "penguinapi")
    if not isinstance(provider, str) or not provider:
        raise PolicyError("provider must be a nonempty string")
    estimate = request.get("estimated_cents")
    # An unpriceable provider cannot be relabelled by supplying a fake zero quote.
    if provider == "penguinapi":
        estimate = None
    elif estimate is not None:
        cents(estimate)
    config = policy(conn)
    limits = (config["company_cents"], config["agents"].get(agent))
    unpriced = estimate is None
    # Legacy explicit flag is an allow policy only before a board policy is installed.
    installed = conn.execute("SELECT 1 FROM budget_policy").fetchone()
    mode = config["unpriced"] if installed else ("allow" if request.get("allow_unpriced") is True else "block")
    if unpriced and mode == "block":
        record_denial(store, run_id=run_id, agent=agent, task_id=task_id,
                      code="UNPRICED_BLOCKED",
                      detail={"provider": provider, "window": window})
        raise BudgetError("UNPRICED_BLOCKED")
    if unpriced:
        company_cap, agent_cap = unpriced_caps()
        company_count = conn.execute(
            "SELECT count(*) FROM budget_reservations WHERE month=? AND unknown_spend=1",
            (window,),
        ).fetchone()[0]
        agent_count = conn.execute(
            "SELECT count(*) FROM budget_reservations WHERE month=? AND unknown_spend=1 AND agent=?",
            (window, agent),
        ).fetchone()[0]
        if company_count >= company_cap or agent_count >= agent_cap:
            record_denial(store, run_id=run_id, agent=agent, task_id=task_id,
                          code="UNPRICED_LIMIT_EXCEEDED",
                          detail={"company_count": company_count, "company_cap": company_cap,
                                  "agent_count": agent_count, "agent_cap": agent_cap,
                                  "window": window})
            raise BudgetError("UNPRICED_LIMIT_EXCEEDED")
    if not unpriced and any(limit is None for limit in limits):
        record_denial(store, run_id=run_id, agent=agent, task_id=task_id,
                      code="BUDGET_NOT_CONFIGURED",
                      detail={"company_limit": limits[0], "agent_limit": limits[1],
                              "estimated_cents": estimate})
        raise BudgetError("BUDGET_NOT_CONFIGURED")
    extras = (0, 0)
    if request.get("budget_override") is not None:
        if unpriced:
            raise PolicyError("budget override requires priced dispatch")
        extras = override_caps(store, conn, request["budget_override"], run_id, task_id, estimate, window)
    current = status(conn, agent, window)
    for layer, limit, extra in zip(("company", "agent"), limits, extras):
        if limit is None:
            continue
        used = current[layer]["known_spend_cents"] + current[layer]["reserved_cents"]
        # Only the known subtotal is compared for unpriced; unknown is not a zero quote.
        projected = used if unpriced else used + estimate
        effective = limit + extra
        if used >= effective or (estimate is not None and used + estimate > effective):
            code = "BUDGET_" + layer.upper() + "_EXCEEDED"
            record_denial(store, run_id=run_id, agent=agent, task_id=task_id, code=code,
                          detail={"layer": layer, "limit_cents": effective,
                                  "used_cents": used, "estimated_cents": estimate,
                                  "unpriced": unpriced, "window": window})
            raise BudgetError(code)
        if projected * 100 >= effective * 80:
            event(conn, "warn", {"layer": layer, "code": "BUDGET_80_PERCENT",
                                 "projected_known_cents": projected,
                                 "unknown_spend": unpriced}, run_id)
    conn.execute("INSERT INTO budget_reservations VALUES(?,?,?,?,?,?,?,?,?)",
                 (run_id, agent, window, estimate, None, "reserved", int(unpriced),
                  os.getpid(), owner_token(os.getpid())))
    detail = {"estimated_cents": estimate, "actual_cents": None, "unknown_spend": unpriced,
              "pricing": "unpriced" if unpriced else "priced", "provider": provider}
    event(conn, "unpriced" if unpriced else "reserved", detail, run_id)
    return detail


def settle(conn, run_id, result, *, fencing_token):
    from .leases import check
    run = conn.execute("SELECT task_id FROM runs WHERE id=?", (run_id,)).fetchone()
    if run is None:
        raise Conflict("missing runtime admission")
    check(conn, run[0], fencing_token, active=True, owner_id=run_id)
    row = conn.execute("SELECT estimated_cents FROM budget_reservations WHERE run_id=? AND status='reserved'",
                       (run_id,)).fetchone()
    if row is None:
        raise Conflict("missing active budget reservation")
    actual = result.get("actual_cents")
    # Unpriced remains unknown even if an adapter reports a fabricated zero.
    if row[0] is None:
        actual = None
    elif actual is not None:
        cents(actual)
    unknown = actual is None
    conn.execute("UPDATE budget_reservations SET actual_cents=?,unknown_spend=?,status='settled' WHERE run_id=?",
                 (actual, int(unknown), run_id))
    event(conn, "settled", {"actual_cents": actual, "unknown_spend": unknown,
                           "released_cents": row[0], "over_estimate": actual is not None and actual > row[0]}, run_id)
    if actual is not None and actual > row[0]:
        event(conn, "estimate_exceeded", {"estimated_cents": row[0], "actual_cents": actual}, run_id)
    return {"actual_cents": actual, "unknown_spend": unknown}


def recover(store):
    """Startup sweep: reclaim only reservations whose owning process is gone."""
    with store.transaction() as conn:
        from .leases import recover as recover_leases
        recover_leases(conn)
        rows = conn.execute("SELECT run_id,owner_pid,owner_token FROM budget_reservations WHERE status='reserved'").fetchall()
        for run_id, pid, token in rows:
            # Leased runs are revoked only by lease recovery, with a new fence.
            if conn.execute("SELECT 1 FROM runs JOIN leases ON leases.task_id=runs.task_id "
                            "WHERE runs.id=?", (run_id,)).fetchone():
                continue
            if owner_token(pid) == token:
                continue
            conn.execute("UPDATE budget_reservations SET status='orphaned',unknown_spend=1 WHERE run_id=?", (run_id,))
            row = conn.execute("SELECT body FROM runs WHERE id=?", (run_id,)).fetchone()
            body = dict(json.loads(row[0]), status="orphaned", unknown_spend=True, ended_at=now())
            conn.execute("UPDATE runs SET status='orphaned',body=? WHERE id=?", (json.dumps(body), run_id))
            event(conn, "orphaned", {"released": True, "unknown_spend": True}, run_id)


def ledger(store):
    """Inspectable records include signed policy history and consumed overrides."""
    with store.connect() as conn:
        events = [{"seq": seq, "at": at, "run_id": run_id, "kind": kind, "detail": json.loads(body)}
                  for seq, at, run_id, kind, body in conn.execute("SELECT * FROM budget_events ORDER BY seq")]
        columns = ["run_id", "agent", "month", "estimated_cents", "actual_cents", "status", "unknown_spend"]
        reservations = [dict(zip(columns, row)) for row in conn.execute(
            "SELECT run_id,agent,month,estimated_cents,actual_cents,status,unknown_spend FROM budget_reservations ORDER BY run_id")]
        denials = [{"seq": seq, "at": at, "run_id": run_id, "agent": agent,
                    "task_id": task_id, "code": code, "detail": json.loads(body)}
                   for seq, at, run_id, agent, task_id, code, body in conn.execute(
                       "SELECT seq,at,run_id,agent,task_id,code,body FROM budget_denials "
                       "ORDER BY seq DESC LIMIT 50")]
        return {"status": status(conn), "reservations": reservations, "events": events,
                "denials": denials}
