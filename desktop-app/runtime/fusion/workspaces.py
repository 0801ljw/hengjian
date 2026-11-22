"""Workspace invitation codes.

The first user in a workspace can invite teammates. Codes are short-lived,
one-use, and scoped to the inviter's workspace. The workspace itself remains
the existing per-user id; this is the minimum team step before full multi-
workspace switching.
"""
from __future__ import annotations

import secrets
import sqlite3
import time
from pathlib import Path


def _conn(instance):
    path = Path(instance) / "fusion.sqlite3"
    conn = sqlite3.connect(path, timeout=5)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS workspaces(
            id TEXT PRIMARY KEY, name TEXT NOT NULL, owner TEXT NOT NULL,
            created_at REAL NOT NULL)""")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS workspace_invites(
            code TEXT PRIMARY KEY, workspace TEXT NOT NULL, created_by TEXT NOT NULL,
            created_at REAL NOT NULL, expires_at REAL NOT NULL, used_at REAL)""")
    return conn


def create_invite(instance, workspace, created_by, ttl=7 * 24 * 3600):
    if not str(workspace).startswith("w_"):
        raise ValueError("invalid workspace")
    ensure(instance, workspace, created_by)
    code = secrets.token_hex(4).upper()
    current = time.time()
    with _conn(instance) as conn:
        conn.execute("INSERT INTO workspace_invites(code,workspace,created_by,created_at,expires_at) VALUES(?,?,?,?,?)",
                     (code, str(workspace)[:80], str(created_by)[:100], current, current + int(ttl)))
    return {"code": code, "workspace": workspace, "expires_at": current + int(ttl), "ttl": int(ttl)}


def consume(instance, code):
    current = time.time()
    with _conn(instance) as conn:
        row = conn.execute("""SELECT workspace,expires_at,used_at FROM workspace_invites
            WHERE code=?""", (str(code or "").strip().upper(),)).fetchone()
        if not row or row[2] is not None or float(row[1]) < current:
            raise ValueError("邀请码无效或已过期")
        conn.execute("UPDATE workspace_invites SET used_at=? WHERE code=?",
                     (current, str(code).strip().upper()))
        return row[0]


def active_invites(instance, workspace):
    current = time.time()
    with _conn(instance) as conn:
        rows = conn.execute("""SELECT code,created_by,created_at,expires_at FROM workspace_invites
            WHERE workspace=? AND used_at IS NULL AND expires_at>? ORDER BY created_at DESC LIMIT 20""",
                            (workspace, current)).fetchall()
    return [{"code": row[0], "created_by": row[1], "created_at": row[2], "expires_at": row[3]}
            for row in rows]


def ensure(instance, workspace, owner):
    current = time.time()
    with _conn(instance) as conn:
        row = conn.execute("SELECT 1 FROM workspaces WHERE id=?", (workspace,)).fetchone()
        if row is None:
            conn.execute("INSERT INTO workspaces(id,name,owner,created_at) VALUES(?,?,?,?)",
                         (workspace, workspace, owner, current))
    return get(instance, workspace)


def get(instance, workspace):
    with _conn(instance) as conn:
        row = conn.execute("SELECT id,name,owner,created_at FROM workspaces WHERE id=?",
                           (workspace,)).fetchone()
    if row is None:
        return None
    return {"id": row[0], "name": row[1], "owner": row[2], "created_at": row[3]}


def members(instance, workspace):
    from .users import UserStore
    users = UserStore(Path(instance) / "auth")
    meta = get(instance, workspace) or {"owner": None, "name": workspace}
    rows = []
    for user in users.users():
        if user.get("workspace") == workspace:
            rows.append({**user, "owner": user["name"] == meta.get("owner")})
    return {"workspace": meta, "members": rows}
