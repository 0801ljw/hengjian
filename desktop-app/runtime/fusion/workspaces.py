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
        CREATE TABLE IF NOT EXISTS workspace_members(
            workspace TEXT NOT NULL, user TEXT NOT NULL, role TEXT NOT NULL,
            joined_at REAL NOT NULL, PRIMARY KEY(workspace,user))""")
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
        row = conn.execute("SELECT owner FROM workspaces WHERE id=?", (workspace,)).fetchone()
        if row is None:
            conn.execute("INSERT INTO workspaces(id,name,owner,created_at) VALUES(?,?,?,?)",
                         (workspace, workspace, owner, current))
            actual_owner = owner
        else:
            actual_owner = row[0]
        conn.execute("""INSERT OR IGNORE INTO workspace_members(workspace,user,role,joined_at)
            VALUES(?,?,?,?)""", (workspace, actual_owner, "owner", current))
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
    by_name = {user["name"]: user for user in users.users()}
    with _conn(instance) as conn:
        rows = conn.execute("""SELECT user,role FROM workspace_members WHERE workspace=?
            ORDER BY joined_at""", (workspace,)).fetchall()
    result = []
    for name, role in rows:
        user = by_name.get(name)
        if user is None:
            continue
        result.append({**user, "workspace_role": role, "owner": name == meta.get("owner")})
    return {"workspace": meta, "members": result}


def add_member(instance, workspace, user, role="member"):
    if role not in ("owner", "admin", "member"):
        raise ValueError("invalid workspace role")
    ensure(instance, workspace, user)
    with _conn(instance) as conn:
        conn.execute("""INSERT INTO workspace_members(workspace,user,role,joined_at) VALUES(?,?,?,?)
            ON CONFLICT(workspace,user) DO UPDATE SET role=excluded.role""",
                     (workspace, user, role, time.time()))


def user_workspaces(instance, user, primary=None):
    if primary:
        ensure(instance, primary, user)
    with _conn(instance) as conn:
        rows = conn.execute("""SELECT w.id,w.name,w.owner,m.role
            FROM workspace_members m JOIN workspaces w ON w.id=m.workspace
            WHERE m.user=? ORDER BY m.joined_at""", (user,)).fetchall()
    return [{"id": row[0], "name": row[1], "owner": row[2], "role": row[3]} for row in rows]


def membership(instance, workspace, user):
    with _conn(instance) as conn:
        row = conn.execute("SELECT role FROM workspace_members WHERE workspace=? AND user=?",
                           (workspace, user)).fetchone()
    return row[0] if row else None


def remove_member(instance, workspace, user):
    with _conn(instance) as conn:
        cur = conn.execute("DELETE FROM workspace_members WHERE workspace=? AND user=?",
                           (workspace, user))
        return cur.rowcount == 1


def leave(instance, workspace, user):
    meta = get(instance, workspace)
    if not meta:
        raise ValueError("workspace not found")
    if meta.get("owner") == user:
        raise ValueError("owner cannot leave; transfer ownership first")
    if not remove_member(instance, workspace, user):
        raise ValueError("not a member of this workspace")
    return user_workspaces(instance, user)


def transfer_owner(instance, workspace, current_owner, new_owner):
    meta = get(instance, workspace)
    if not meta or meta.get("owner") != current_owner:
        raise ValueError("only the workspace owner can transfer")
    if not membership(instance, workspace, new_owner):
        raise ValueError("new owner must be a member")
    with _conn(instance) as conn:
        conn.execute("UPDATE workspaces SET owner=? WHERE id=?", (new_owner, workspace))
        conn.execute("""INSERT INTO workspace_members(workspace,user,role,joined_at) VALUES(?,?,?,?)
            ON CONFLICT(workspace,user) DO UPDATE SET role='owner'""",
                     (workspace, new_owner, "owner", time.time()))
        conn.execute("UPDATE workspace_members SET role='admin' WHERE workspace=? AND user=?",
                     (workspace, current_owner))
    return get(instance, workspace)


def set_role(instance, workspace, actor, target, role):
    if role not in ("admin", "member"):
        raise ValueError("role must be admin or member")
    meta = get(instance, workspace)
    if not meta:
        raise ValueError("workspace not found")
    actor_role = membership(instance, workspace, actor)
    if actor_role not in ("owner", "admin"):
        raise ValueError("workspace owner/admin required")
    if target == meta.get("owner"):
        raise ValueError("cannot change the owner's role")
    if not membership(instance, workspace, target):
        raise ValueError("member not found")
    with _conn(instance) as conn:
        conn.execute("UPDATE workspace_members SET role=? WHERE workspace=? AND user=?",
                     (role, workspace, target))
    return members(instance, workspace)


def rename(instance, workspace, actor, name):
    text = str(name or "").strip()
    if not (2 <= len(text) <= 60):
        raise ValueError("workspace name must be 2..60 chars")
    actor_role = membership(instance, workspace, actor)
    if actor_role not in ("owner", "admin"):
        raise ValueError("workspace owner/admin required")
    with _conn(instance) as conn:
        conn.execute("UPDATE workspaces SET name=? WHERE id=?", (text, workspace))
    return get(instance, workspace)
