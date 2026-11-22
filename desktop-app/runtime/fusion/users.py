"""Human identity for the fusion server: users, password hashing, sessions.

Machine clients keep using bearer API tokens (fusion.httpapi). Humans log in
here and receive a short-lived session token that the server maps to a role.

Storage lives under the instance home:
  users.json    { "users": [ {name, salt, hash, role, created_at} ] }   0600
  sessions.json { "sessions": [ {token_sha256, name, role, expires_at} ] } 0600
Passwords are scrypt-hashed with a per-user salt; plaintext is never stored.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path

ROLES = ("admin", "operator", "viewer")
_SCRYPT = dict(n=2 ** 14, r=8, p=1, dklen=32)


class AuthError(Exception):
    pass


def _atomic_write(path: Path, obj) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def _hash_password(password: str, salt: bytes) -> str:
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, **_SCRYPT).hex()


class UserStore:
    def __init__(self, home: Path):
        self.home = Path(home)
        self.home.mkdir(parents=True, exist_ok=True)
        self.path = self.home / "users.json"
        self.sessions_path = self.home / "sessions.json"

    # ── users ────────────────────────────────────────────────────────────
    def _load(self) -> list[dict]:
        if not self.path.exists():
            return []
        data = json.loads(self.path.read_text(encoding="utf-8"))
        users = data.get("users")
        if not isinstance(users, list):
            raise AuthError("users.json is malformed")
        return users

    def users(self) -> list[dict]:
        return [{"name": u["name"], "role": u["role"], "created_at": u["created_at"],
                 "workspace": u.get("workspace", "w_legacy")} for u in self._load()]

    def workspace(self, name: str) -> str:
        for user in self._load():
            if user["name"] == name:
                return user.get("workspace", "w_legacy")
        return "w_legacy"

    def add_user(self, name: str, password: str, role: str = "operator",
                 workspace: str | None = None) -> None:
        if role not in ROLES:
            raise AuthError(f"role must be one of {ROLES}")
        if not (3 <= len(name) <= 64) or not name.replace("_", "").replace("-", "").isalnum():
            raise AuthError("invalid user name")
        if len(password) < 8:
            raise AuthError("password must be at least 8 characters")
        users = self._load()
        if any(u["name"] == name for u in users):
            raise AuthError("user already exists")
        salt = secrets.token_bytes(16)
        if workspace is None:
            workspace = "w_" + hashlib.sha256(name.encode()).hexdigest()[:16]
        if not isinstance(workspace, str) or not workspace.startswith("w_") or len(workspace) > 80:
            raise AuthError("invalid workspace")
        users.append({"name": name, "salt": salt.hex(),
                      "hash": _hash_password(password, salt), "role": role,
                      "workspace": workspace,
                      "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
        _atomic_write(self.path, {"users": users})

    def set_primary_workspace(self, name: str, workspace: str) -> None:
        users = self._load()
        for user in users:
            if user["name"] == name:
                user["workspace"] = workspace
                _atomic_write(self.path, {"users": users})
                return
        raise AuthError("user not found")

    def remove_user(self, name: str) -> None:
        users = [u for u in self._load() if u["name"] != name]
        _atomic_write(self.path, {"users": users})
        sessions = [s for s in self._load_sessions() if s.get("name") != name]
        _atomic_write(self.sessions_path, {"sessions": sessions})

    def verify(self, name: str, password: str) -> str | None:
        """Return the role on success, None otherwise (constant-time compare)."""
        for u in self._load():
            if u["name"] != name:
                continue
            digest = _hash_password(password, bytes.fromhex(u["salt"]))
            if hmac.compare_digest(digest, u["hash"]):
                return u["role"]
            return None
        return None

    # ── sessions ─────────────────────────────────────────────────────────
    def _load_sessions(self) -> list[dict]:
        if not self.sessions_path.exists():
            return []
        data = json.loads(self.sessions_path.read_text(encoding="utf-8"))
        sessions = data.get("sessions")
        if not isinstance(sessions, list):
            raise AuthError("sessions.json is malformed")
        now = time.time()
        return [s for s in sessions if float(s.get("expires_at", 0)) > now]

    def login(self, name: str, password: str, *, ttl_sec: int = 8 * 3600) -> dict:
        role = self.verify(name, password)
        if role is None:
            raise AuthError("invalid credentials")
        token = secrets.token_urlsafe(32)
        sessions = self._load_sessions()
        sessions.append({"token_sha256": hashlib.sha256(token.encode()).hexdigest(),
                         "name": name, "role": role, "workspace": self.workspace(name),
                         "expires_at": time.time() + ttl_sec})
        _atomic_write(self.sessions_path, {"sessions": sessions})
        return {"token": token, "name": name, "role": role,
                "workspace": self.workspace(name),
                "expires_at": int(time.time() + ttl_sec)}

    def check_session(self, token: str) -> dict | None:
        digest = hashlib.sha256(token.encode()).hexdigest()
        users = {u["name"]: u for u in self._load()}
        for s in self._load_sessions():
            if hmac.compare_digest(s["token_sha256"], digest):
                user = users.get(s["name"])
                if user is None:
                    return None
                return {"name": user["name"], "role": user["role"],
                        "workspace": s.get("workspace") or user.get("workspace", "w_legacy")}
        return None

    def switch_workspace(self, token: str, workspace: str) -> bool:
        digest = hashlib.sha256(token.encode()).hexdigest()
        sessions = self._load_sessions()
        found = False
        for session in sessions:
            if hmac.compare_digest(session["token_sha256"], digest):
                session["workspace"] = workspace
                found = True
                break
        if found:
            _atomic_write(self.sessions_path, {"sessions": sessions})
        return found

    def logout(self, token: str) -> bool:
        digest = hashlib.sha256(token.encode()).hexdigest()
        sessions = self._load_sessions()
        kept = [s for s in sessions if s["token_sha256"] != digest]
        _atomic_write(self.sessions_path, {"sessions": kept})
        return len(kept) != len(sessions)


__all__ = ["UserStore", "AuthError", "ROLES"]
