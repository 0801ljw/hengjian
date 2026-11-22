"""Single-host leases and transactional fencing; no unconditional takeover."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time

from .budget import owner_token
from .core import Conflict, now

TTL = 30.0
HEARTBEAT = 10.0
# Capabilities are issued at admission, never reconstructed from database reads.
_held = {}


class LeaseError(Conflict):
    pass


def initialize(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS leases(
            task_id TEXT PRIMARY KEY REFERENCES tasks(id), owner_id TEXT,
            fencing_token INTEGER NOT NULL CHECK(fencing_token>0),
            acquired_at REAL NOT NULL, expires_at REAL NOT NULL,
            heartbeat_at REAL NOT NULL, owner_pid INTEGER,
            process_identity TEXT);
        CREATE TABLE IF NOT EXISTS execution_commits(
            task_id TEXT PRIMARY KEY REFERENCES tasks(id),
            run_id TEXT UNIQUE NOT NULL REFERENCES runs(id),
            fencing_token INTEGER NOT NULL, artifact TEXT NOT NULL);
    """)


def read(conn, task_id):
    cursor = conn.execute("SELECT * FROM leases WHERE task_id=?", (task_id,))
    row = cursor.fetchone()
    return dict(zip([c[0] for c in cursor.description], row)) if row else None


def alive(lease):
    """Fail closed on inaccessible processes; require both PID and identity."""
    try:
        os.kill(lease["owner_pid"], 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        return owner_token(lease["owner_pid"]) == lease["process_identity"]
    except OSError:
        return True


def check(conn, task_id, token, *, active=False, owner_id=None):
    lease = read(conn, task_id)
    current = lease["fencing_token"] if lease else 0
    if type(token) is not int or token != current:
        raise LeaseError(f"STALE_FENCING_TOKEN: supplied={token}, current={current}")
    if active and (not lease or lease["owner_id"] != owner_id
                   or lease["expires_at"] < time.time()):
        raise LeaseError("LEASE_NOT_HELD: expired, released or wrong owner")
    if lease and lease["owner_id"] and lease["expires_at"] < time.time():
        raise LeaseError("LEASE_EXPIRED: write refused")
    return lease


def _revoke(conn, lease, *, expired=False):
    """A controller revocation advances the fence BEFORE touching old state."""
    task_id, token = lease["task_id"], lease["fencing_token"]
    current = read(conn, task_id)
    if not current or current != lease or not current["owner_id"]:
        raise LeaseError("STALE_FENCING_TOKEN: revocation CAS failed")
    if expired:
        if current["expires_at"] >= time.time():
            raise LeaseError("LEASE_BUSY: revocation requires expiry")
    elif alive(current):
        raise LeaseError("OWNER_ALIVE: recovery refused")
    conn.execute("UPDATE leases SET owner_id=NULL,owner_pid=NULL,process_identity=NULL,"
                 "fencing_token=?,expires_at=0 WHERE task_id=? AND fencing_token=?",
                 (token + 1, task_id, token))
    check(conn, task_id, token + 1)
    rows = conn.execute("SELECT id,body FROM runs WHERE task_id=? AND status='running'",
                        (task_id,)).fetchall()
    from .budget import event
    for run_id, body in rows:
        data = dict(json.loads(body), status="orphaned", unknown_spend=True,
                    ended_at=now(), revoked_by_fencing_token=token + 1)
        conn.execute("UPDATE runs SET status='orphaned',body=? WHERE id=?",
                     (json.dumps(data), run_id))
        conn.execute("UPDATE budget_reservations SET status='orphaned',unknown_spend=1 "
                     "WHERE run_id=? AND status='reserved'", (run_id,))
        event(conn, "orphaned", {"released": True, "unknown_spend": True,
                                 "fencing_token": token + 1}, run_id)


def acquire(conn, task_id, run_id):
    """Caller holds BEGIN IMMEDIATE; budget/run/lease share its savepoint."""
    lease = read(conn, task_id)
    timestamp = time.time()
    if lease and lease["owner_id"]:
        if lease["expires_at"] >= timestamp:
            raise LeaseError("LEASE_BUSY: unexpired owner; use dead-owner recovery")
        _revoke(conn, lease, expired=True)
        lease = read(conn, task_id)
    token = lease["fencing_token"] + 1 if lease else 1
    conn.execute("INSERT INTO leases VALUES(?,?,?,?,?,?,?,?) "
                 "ON CONFLICT(task_id) DO UPDATE SET owner_id=excluded.owner_id,"
                 "fencing_token=excluded.fencing_token,acquired_at=excluded.acquired_at,"
                 "expires_at=excluded.expires_at,heartbeat_at=excluded.heartbeat_at,"
                 "owner_pid=excluded.owner_pid,process_identity=excluded.process_identity",
                 (task_id, run_id, token, timestamp, timestamp + TTL, timestamp,
                  os.getpid(), owner_token(os.getpid())))
    return token


def renew(store, task_id, run_id, token):
    with store.transaction() as conn:
        lease = check(conn, task_id, token, active=True, owner_id=run_id)
        if lease["owner_pid"] != os.getpid() or lease["process_identity"] != owner_token(os.getpid()):
            raise LeaseError("LEASE_WRONG_PROCESS")
        timestamp = time.time()
        conn.execute("UPDATE leases SET heartbeat_at=?,expires_at=? WHERE task_id=?",
                     (timestamp, timestamp + TTL, task_id))


def release(conn, task_id, run_id, token):
    check(conn, task_id, token, active=True, owner_id=run_id)
    # Keep the fence forever, including after normal completion.
    conn.execute("UPDATE leases SET owner_id=NULL,owner_pid=NULL,process_identity=NULL,"
                 "expires_at=0 WHERE task_id=?", (task_id,))


def force_release(store, task_id, token):
    with store.transaction() as conn:
        lease = read(conn, task_id)
        if not lease or type(token) is not int or token != lease["fencing_token"]:
            raise LeaseError("STALE_FENCING_TOKEN")
        if not lease["owner_id"]:
            raise LeaseError("LEASE_NOT_HELD")
        try:
            os.kill(lease["owner_pid"], 0)
            pid_exists = True
        except ProcessLookupError:
            pid_exists = False
        except PermissionError:
            pid_exists = True
        if pid_exists:
            raise LeaseError(f"OWNER_ALIVE: force-release refused for PID {lease['owner_pid']}")
        _revoke(conn, lease)


def recover(conn):
    for (task_id,) in conn.execute("SELECT task_id FROM leases WHERE owner_id IS NOT NULL").fetchall():
        lease = read(conn, task_id)
        if not alive(lease):
            _revoke(conn, lease)


def remember(store, task_id, run_id, token):
    store._fences[task_id] = token
    stop = threading.Event()
    _held[(str(store.db), run_id, os.getpid())] = (token, stop)

    def heartbeat():
        while not stop.wait(HEARTBEAT):
            try:
                renew(store, task_id, run_id, token)
            except (LeaseError, sqlite3.Error, OSError):
                # Lost lease is terminal for this holder; every later write fails closed.
                stop.set()

    threading.Thread(target=heartbeat, name=f"lease-{run_id}", daemon=True).start()


def held_token(store, run_id):
    return _held.get((str(store.db), run_id, os.getpid()), (None, None))[0]


def stop_heartbeat(store, run_id):
    held = _held.pop((str(store.db), run_id, os.getpid()), None)
    if held:
        held[1].set()
