"""Cloud-side Launcher pairing and heartbeat.

A human signed into the Workspace creates a short-lived pairing code. The
Launcher exchanges it for a bearer token and then heartbeats. This is the
first slice of the OpenAgents-style Launcher <-> Workspace link; it does not
yet dispatch tasks.
"""
from __future__ import annotations

import hashlib
import secrets
import sqlite3
import time
import uuid
from pathlib import Path

from .core import now


def _conn(instance):
    path = Path(instance) / "fusion.sqlite3"
    conn = sqlite3.connect(path, timeout=5)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS launchers(
            id TEXT PRIMARY KEY, owner TEXT NOT NULL, name TEXT NOT NULL,
            host TEXT, version TEXT, created_at TEXT NOT NULL,
            last_seen TEXT, token_hash TEXT NOT NULL)""")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS launcher_codes(
            code TEXT PRIMARY KEY, owner TEXT NOT NULL, created_at REAL NOT NULL,
            expires_at REAL NOT NULL)""")
    return conn


def _hash(token):
    return hashlib.sha256(str(token).encode()).hexdigest()


def create_pairing_code(instance, owner, ttl=600):
    code = secrets.token_hex(4).upper()
    current = time.time()
    with _conn(instance) as conn:
        conn.execute("DELETE FROM launcher_codes WHERE expires_at < ?", (current,))
        conn.execute("INSERT INTO launcher_codes(code,owner,created_at,expires_at) VALUES(?,?,?,?)",
                     (code, str(owner)[:100], current, current + int(ttl)))
    return {"code": code, "expires_at": current + int(ttl), "ttl": int(ttl)}


def claim(instance, code, name, host="", version=""):
    current = time.time()
    with _conn(instance) as conn:
        row = conn.execute("SELECT owner,expires_at FROM launcher_codes WHERE code=?",
                           (str(code).strip().upper(),)).fetchone()
        if not row or float(row[1]) < current:
            raise ValueError("配对码无效或已过期")
        conn.execute("DELETE FROM launcher_codes WHERE code=?", (str(code).strip().upper(),))
        launcher_id = uuid.uuid4().hex
        token = secrets.token_urlsafe(24)
        conn.execute("""INSERT INTO launchers(id,owner,name,host,version,created_at,last_seen,token_hash)
            VALUES(?,?,?,?,?,?,?,?)""",
                     (launcher_id, row[0], str(name or "Hengjian Launcher")[:80],
                      str(host or "")[:200], str(version or "")[:50], now(), now(), _hash(token)))
    return {"launcher_id": launcher_id, "token": token, "workspace": "w_local"}


def list_launchers(instance, owner=None):
    with _conn(instance) as conn:
        if owner is None:
            rows = conn.execute("""SELECT id,owner,name,host,version,created_at,last_seen
                FROM launchers ORDER BY last_seen DESC LIMIT 100""").fetchall()
        else:
            rows = conn.execute("""SELECT id,owner,name,host,version,created_at,last_seen
                FROM launchers WHERE owner=? ORDER BY last_seen DESC LIMIT 100""",
                                (owner,)).fetchall()
    result = []
    for row in rows:
        result.append({"id": row[0], "owner": row[1], "name": row[2], "host": row[3],
                       "version": row[4], "created_at": row[5], "last_seen": row[6]})
    return result


def exists(instance, launcher_id):
    with _conn(instance) as conn:
        return conn.execute("SELECT 1 FROM launchers WHERE id=?", (launcher_id,)).fetchone() is not None


def authenticate(instance, token):
    with _conn(instance) as conn:
        rows = conn.execute("SELECT id,token_hash FROM launchers").fetchall()
    for launcher_id, token_hash in rows:
        if secrets.compare_digest(token_hash, _hash(token)):
            return launcher_id
    return None


def heartbeat(instance, launcher_id, token):
    with _conn(instance) as conn:
        row = conn.execute("SELECT token_hash FROM launchers WHERE id=?", (launcher_id,)).fetchone()
        if not row or not secrets.compare_digest(row[0], _hash(token)):
            return False
        conn.execute("UPDATE launchers SET last_seen=? WHERE id=?", (now(), launcher_id))
        return True


def revoke(instance, launcher_id, owner=None):
    with _conn(instance) as conn:
        if owner is None:
            cur = conn.execute("DELETE FROM launchers WHERE id=?", (launcher_id,))
        else:
            cur = conn.execute("DELETE FROM launchers WHERE id=? AND owner=?", (launcher_id, owner))
        return cur.rowcount == 1
