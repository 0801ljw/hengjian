"""Fusion HTTP server: mounts the authenticated API and the web UI.

Binds to loopback by default. All /api/* requests go through fusion.httpapi,
which enforces bearer-token auth and writes a per-request audit line. Static
assets are served from fusion/web when present.

This module only wires existing components; it holds no business logic.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import secrets
import socketserver
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .core import Store, atomic_json_write
from .httpapi import API, worker_error_summary
from .model_access import ModelAccess
from .sync_journal import SyncJournal
from .sync_projection import project_store
from .issues import IssueStore
from .registration import allow as registration_allow
from . import ai_view, onboarding
from .users import AuthError, UserStore

WEB_DIR = Path(__file__).resolve().parent / "web"
logger = logging.getLogger(__name__)
PUBLIC_PATHS = {"/api/health"}
LOGIN_PATH = "/api/login"
AUTH_STATUS_PATH = "/api/auth/status"
REGISTER_PATH = "/api/register"
LOGOUT_PATH = "/api/logout"
ME_PATH = "/api/me"
# 浏览器会话：httpOnly cookie（JS 读不到，XSS 偷不走）
SESSION_COOKIE = "fusion_session"
SESSION_COOKIE_TTL = 8 * 3600
# cookie 会被浏览器自动携带，所以状态变更必须带自定义头（强制预检，挡 CSRF）
CSRF_HEADER = "X-Fusion-Request"
CSRF_HEADER_VALUE = "1"
MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
# role → methods it may use on /api/* (machine API tokens bypass this)
ROLE_METHODS = {"viewer": {"GET"}, "operator": {"GET", "POST"}, "admin": {"GET", "POST", "DELETE", "PUT", "PATCH"}}


ARTIFACT_TEXT_SUFFIXES = {".md", ".txt", ".json", ".csv", ".yaml", ".yml"}
ARTIFACT_MAX_BYTES = 64 * 1024


def _request_ip(handler):
    """Use the Cloudflare client IP only when the peer is the local tunnel."""
    peer = handler.client_address[0]
    if os.environ.get("DUTANG_TRUST_PROXY") == "1" and peer in ("127.0.0.1", "::1"):
        candidate = (handler.headers.get("CF-Connecting-IP")
                     or (handler.headers.get("X-Forwarded-For") or "").split(",", 1)[0].strip())
        try:
            ipaddress.ip_address(candidate)
            return candidate
        except ValueError:
            pass
    return peer


def _registration_allowed(peer, host, origin):
    """First-account registration is loopback-only unless explicitly opted in.

    ``DUTANG_ALLOW_REMOTE_REGISTRATION=1`` opens remote peers (dangerous);
    ``DUTANG_ALLOW_PRIVATE_REGISTRATION=1`` is the narrow Docker-bridge opt-in.
    The peer address is the identity; Host is only a DNS-rebinding check.
    """
    if os.environ.get("DUTANG_DISABLE_REGISTRATION") == "1":
        return False
    if os.environ.get("DUTANG_ALLOW_REMOTE_REGISTRATION") == "1":
        allowed_peer = True
    else:
        try:
            address = ipaddress.ip_address(str(peer).split("%", 1)[0])
            allowed_peer = address.is_loopback or (
                address.is_private
                and os.environ.get("DUTANG_ALLOW_PRIVATE_REGISTRATION") == "1")
        except ValueError:
            allowed_peer = False
    if not allowed_peer:
        return False
    try:
        authority = urlsplit("http://" + str(host or ""))
        local_host = authority.hostname in {"127.0.0.1", "localhost", "::1"}
        if os.environ.get("DUTANG_ALLOW_REMOTE_REGISTRATION") != "1" and not local_host:
            return False
        if origin:
            source = urlsplit(origin)
            if source.scheme not in ("http", "https") or source.netloc != host:
                return False
    except ValueError:
        return False
    return True


def _workspace(store, run_id: str) -> Path:
    """Resolve a run workspace, refusing anything that is not a plain child."""
    if not run_id or "/" in run_id or "\\" in run_id or run_id.startswith("."):
        raise ValueError("invalid run id")
    workspace = (store.home / "runs" / run_id).resolve()
    if not workspace.is_dir() or not workspace.is_relative_to(store.home.resolve()):
        raise ValueError("unknown run")
    return workspace


def list_artifacts(store, run_id: str) -> list[dict]:
    """Files this run wrote, taken from its own manifest."""
    workspace = _workspace(store, run_id)
    manifest = workspace / "manifest.json"
    if not manifest.is_file():
        return []
    data = json.loads(manifest.read_text(encoding="utf-8"))
    primary_subtask = data.get("primary_subtask")
    out = []
    for position, item in enumerate(data.get("files") or []):
        raw = str(item.get("path", ""))
        path = Path(raw).resolve()
        if not path.is_file() or not path.is_relative_to(workspace):
            continue
        subtask = item.get("subtask")
        if item.get("primary") is not None:
            primary = bool(item["primary"])
        elif primary_subtask is not None:
            primary = subtask == primary_subtask
        else:
            # Older manifests carry no subtask attribution: fall back to the last
            # written file and say so rather than pretending certainty.
            primary = position == len(data.get("files") or []) - 1
        out.append({"name": path.name, "relative": str(path.relative_to(workspace)),
                    "bytes": path.stat().st_size, "sha256": item.get("sha256"),
                    "subtask": subtask, "primary": primary,
                    "attribution": "manifest" if subtask is not None else "inferred",
                    "previewable": path.suffix.lower() in ARTIFACT_TEXT_SUFFIXES
                                   and path.stat().st_size <= ARTIFACT_MAX_BYTES})
    return out


def read_artifact(store, run_id: str, relative: str) -> tuple[str, str]:
    """Return (text, sha256) for one previewable artifact."""
    workspace = _workspace(store, run_id)
    if not relative or relative.startswith("/") or ".." in Path(relative).parts:
        raise ValueError("invalid artifact name")
    path = (workspace / relative).resolve()
    if not path.is_file() or not path.is_relative_to(workspace):
        raise ValueError("unknown artifact")
    if path.suffix.lower() not in ARTIFACT_TEXT_SUFFIXES:
        raise ValueError("artifact type is not previewable")
    if path.stat().st_size > ARTIFACT_MAX_BYTES:
        raise ValueError("artifact is too large to preview")
    data = path.read_bytes()
    return data.decode("utf-8", "replace"), hashlib.sha256(data).hexdigest()


class Handler(BaseHTTPRequestHandler):
    server_version = "fusion/1.0"

    def log_message(self, fmt, *args):  # keep stdout quiet during tests
        if getattr(self.server, "verbose", False):
            super().log_message(fmt, *args)

    def _send(self, status: int, obj, headers: dict | None = None) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    # ── 会话 cookie 工具 ──────────────────────────────────────────────────
    def _cookie(self, name: str) -> str:
        raw = str(self.headers.get("Cookie") or "")
        for part in raw.split(";"):
            key, _, value = part.strip().partition("=")
            if key == name:
                return value
        return ""

    def _session_cookie_header(self, token: str) -> dict:
        return {"Set-Cookie": "%s=%s; Path=/; HttpOnly; SameSite=Strict; Max-Age=%d"
                % (SESSION_COOKIE, token, SESSION_COOKIE_TTL)}

    def _clear_session_cookie_header(self) -> dict:
        return {"Set-Cookie": "%s=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0"
                % SESSION_COOKIE}

    def _csrf_ok(self) -> bool:
        """cookie 会话的状态变更必须带自定义头。

        跨站请求无法在"简单请求"里加自定义头，加头就会触发预检；配合
        SameSite=Strict，两层挡住 CSRF。Bearer 认证（机器/CLI）不需要。
        """
        if str(self.headers.get(CSRF_HEADER) or "") == CSRF_HEADER_VALUE:
            return True
        return False

    def _send_file(self, path: Path) -> None:
        data = path.read_bytes()
        ctype = {".html": "text/html; charset=utf-8",
                 ".js": "application/javascript; charset=utf-8",
                 ".css": "text/css; charset=utf-8"}.get(path.suffix, "application/octet-stream")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._handle("GET", b"")

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        self._handle("POST", body)

    def do_DELETE(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        self._handle("DELETE", body)

    def _handle(self, method: str, body: bytes) -> None:
        path = self.path.split("?", 1)[0]
        bearer = str(self.headers.get("Authorization") or "")
        raw_token = bearer[len("Bearer "):].strip() if bearer.startswith("Bearer ") else ""
        cookie_token = self._cookie(SESSION_COOKIE)
        self._session_token = raw_token or cookie_token
        self._session_source = ""
        self._session = self.server.users.check_session(raw_token) if raw_token else None
        if self._session:
            self._session_source = "bearer"
        elif cookie_token:
            self._session = self.server.users.check_session(cookie_token)
            self._session_source = "cookie" if self._session else ""

        # CSRF：只有 cookie 会话需要自定义头；Bearer 不受影响。
        if (self._session_source == "cookie" and method in MUTATING_METHODS
                and path.startswith("/api/") and not self._csrf_ok()):
            return self._send(403, {"error": "missing %s header" % CSRF_HEADER})

        # static UI
        if method == "GET" and not path.startswith("/api/"):
            if path in ("/login", "/login/"):
                rel = "login.html"
            elif path in ("/", ""):
                rel = "onboarding.html" if onboarding.required(self.server.store.home) else "index.html"
            else:
                rel = path.lstrip("/")
            target = (WEB_DIR / rel).resolve()
            if WEB_DIR in target.parents or target == WEB_DIR / "index.html":
                if target.is_file():
                    return self._send_file(target)
            return self._send(404, {"error": "not found"})

        # This exception is scoped to onboarding, never business/installation APIs.
        if path in onboarding.PATHS:
            try:
                if not onboarding.allowed(self.server.users, self._session,
                                          self.client_address[0],
                                          self.headers.get("Host", ""),
                                          self.headers.get("Origin")):
                    return self._send(403, {"error": "admin session or local bootstrap required"})
                with self.server.onboarding_lock:
                    status, payload = onboarding.handle(self.server.store.home, method, path, body)
            except (OSError, ValueError, AuthError):
                status, payload = 503, {"error": "onboarding unavailable"}
            return self._send(status, payload)

        # Model access: provider metadata is public; saved records/keys are
        # admin-session-only. Mounted explicitly so the UI's /api/models calls
        # do not fall through to the generic API router.
        if path == "/api/models" or path.startswith("/api/models/"):
            try:
                status, payload = self.server.model_access.handle(
                    method, path, body, self._session)
            except Exception:
                status, payload = 503, {"error": "model access unavailable"}
            return self._send(status, payload)

        # Read-only sync journal: same auth boundary as the ledger.
        if path in ("/api/sync/snapshot", "/api/sync/events"):
            session_ok = bool(getattr(self, "_session", None))
            status, _ = self.server.api.handle("GET", "/api/health", dict(self.headers), b"")
            if not session_ok and status != 200:
                return self._send(401, {"error": "unauthorized"})
            query = parse_qs(urlsplit(self.path).query)
            workspace = (query.get("workspace") or ["w_local"])[0]
            journal = getattr(self.server, "sync_journal", None)
            if journal is None:
                return self._send(503, {"error": "sync journal unavailable"})
            if path == "/api/sync/snapshot":
                return self._send(200, journal.snapshot(workspace))
            try:
                after = int((query.get("after") or ["0"])[0])
                limit = int((query.get("limit") or ["200"])[0])
                return self._send(200, journal.after(workspace, after, limit))
            except (TypeError, ValueError):
                return self._send(400, {"error": "invalid cursor or limit"})

        # AI decision timeline (read-only; a session or a machine token may read it)
        if method == "GET" and (path == "/api/ai-runs" or path.startswith("/api/ai-runs/")):
            session_ok = bool(getattr(self, "_session", None))
            status, payload = self.server.api.handle(
                method, "/api/health", dict(self.headers), b"")  # machine-token probe
            if not session_ok and status != 200:
                return self._send(status, payload)
            try:
                runs = ai_view.read_runs(self.server.store)
                if path == "/api/ai-runs":
                    data = {"runs": runs}
                else:
                    want = path.rsplit("/", 1)[-1]
                    hit = [r for r in runs if str(r.get("run_id")) == want]
                    data = hit[0] if hit else None
            except Exception as exc:  # noqa: BLE001 - surfaced to the caller
                return self._send(500, {"error": str(exc)})
            if data is None:
                return self._send(404, {"error": "not found"})
            return self._send(200, data)

        if method == "GET" and path in ("/api/artifacts", "/api/artifact"):
            query = parse_qs(urlsplit(self.path).query)
            run_id = (query.get("run") or [""])[0]
            session_ok = bool(getattr(self, "_session", None))
            status, _ = self.server.api.handle("GET", "/api/health", dict(self.headers), b"")
            if not session_ok and status != 200:
                return self._send(401, {"error": "unauthorized"})
            try:
                if path == "/api/artifacts":
                    workspace = _workspace(self.server.store, run_id)
                    request = workspace / "board-request.json"
                    return self._send(200, {
                        "artifacts": list_artifacts(self.server.store, run_id),
                        "board_request": {"relative": "board-request.json",
                                          "exists": request.is_file()}})
                text, digest = read_artifact(self.server.store, run_id,
                                             (query.get("name") or [""])[0])
                return self._send(200, {"text": text, "sha256": digest})
            except (ValueError, OSError) as exc:
                return self._send(404 if "unknown" in str(exc) else 400, {"error": str(exc)})

        if path in PUBLIC_PATHS:
            self._send(200, {"ok": True, "service": "fusion"})
            return

        users = self.server.users
        if method == "GET" and path == AUTH_STATUS_PATH:
            return self._send(200, {
                "users": bool(users.users()),
                "onboarding_required": onboarding.required(self.server.store.home),
            })
        if method == "POST" and path == REGISTER_PATH:
            client_ip = _request_ip(self)
            if not _registration_allowed(client_ip, self.headers.get("Host", ""),
                                         self.headers.get("Origin")):
                return self._send(403, {"error": "registration is local-only"})
            public = os.environ.get("DUTANG_PUBLIC_REGISTRATION") == "1"
            if public and users.users():
                if len(users.users()) >= 1000:
                    return self._send(403, {"error": "registration is full"})
                if not registration_allow(client_ip):
                    return self._send(429, {"error": "too many registration attempts"})
            try:
                data = json.loads(body.decode("utf-8") or "{}")
                if not isinstance(data, dict) or set(data) - {"username", "password", "invite"}:
                    raise AuthError("invalid registration payload")
            except (ValueError, UnicodeError):
                return self._send(400, {"error": "invalid registration payload"})
            invite = str(data.get("invite") or "").strip()
            # After the first account, adding members is an admin action unless
            # public registration is enabled or the user presents a valid invite.
            if users.users() and not public and not invite:
                if not self._session or self._session.get("role") != "admin":
                    return self._send(403, {"error": "admin session required"})
            bootstrap = os.environ.get("DUTANG_BOOTSTRAP_TOKEN")
            if bootstrap and not users.users():
                supplied = self.headers.get("X-Fusion-Bootstrap", "")
                if not hmac.compare_digest(str(supplied), bootstrap):
                    return self._send(403, {"error": "bootstrap token required"})
            try:
                invited_workspace = None
                if invite:
                    from .workspaces import consume
                    invited_workspace = consume(self.server.store.home, invite)
                username = str(data.get("username", ""))
                password = str(data.get("password", ""))
                # First account bootstraps the admin; later accounts are
                # ordinary workspace members. Public signup can never create
                # an admin; role is operator/viewer only.
                if not users.users():
                    role = "admin"
                else:
                    role = os.environ.get("DUTANG_REGISTRATION_ROLE", "operator")
                    if role not in ("operator", "viewer"):
                        role = "operator"
                users.add_user(username, password, role, workspace=invited_workspace)
                session = users.login(username, password)
                from .workspaces import add_member, ensure
                ensure(self.server.store.home, session["workspace"], username)
                add_member(self.server.store.home, session["workspace"], username,
                           "member" if invited_workspace else "owner")
            except (AuthError, ValueError, UnicodeError):
                return self._send(400, {"error": "用户名或密码不符合要求，或用户已存在"})
            return self._send(200, {"session": session["token"], "name": session["name"],
                                    "role": session["role"], "workspace": session.get("workspace"),
                                    "expires_at": session["expires_at"]},
                              headers=self._session_cookie_header(session["token"]))
        if method == "POST" and path == LOGIN_PATH:
            try:
                data = json.loads(body.decode("utf-8") or "{}")
                session = users.login(str(data.get("username", "")), str(data.get("password", "")))
                from .workspaces import ensure
                ensure(self.server.store.home, session["workspace"], session["name"])
            except (AuthError, ValueError):
                return self._send(401, {"error": "invalid credentials"})
            return self._send(200, {"session": session["token"], "name": session["name"],
                                    "role": session["role"], "workspace": session.get("workspace"),
                                    "expires_at": session["expires_at"]},
                              headers=self._session_cookie_header(session["token"]))

        # 会话已在 _handle 开头解析（bearer 优先，其次 cookie）
        raw = self._session_token
        session = self._session
        workspace_id = (session or {}).get("workspace", "w_legacy")

        if path == "/api/launchers/claim" and method == "POST":
            if not registration_allow(_request_ip(self)):
                return self._send(429, {"error": "too many pairing attempts"})
            try:
                data = json.loads(body or b"{}")
                from .launchers import claim
                return self._send(200, claim(self.server.store.home, data.get("code", ""),
                                             data.get("name", ""), data.get("host", ""),
                                             data.get("version", "")))
            except (ValueError, TypeError) as exc:
                return self._send(400, {"error": str(exc)})
        if path == "/api/launchers/login" and method == "POST":
            if not registration_allow(_request_ip(self)):
                return self._send(429, {"error": "too many launcher login attempts"})
            try:
                data = json.loads(body or b"{}")
                from .launchers import login_with_credentials
                return self._send(200, login_with_credentials(
                    self.server.store.home, data.get("username", ""), data.get("password", ""),
                    data.get("name", ""), data.get("host", ""), data.get("version", "")))
            except (ValueError, TypeError) as exc:
                return self._send(401, {"error": str(exc)})

        if path == "/api/launchers/pair" and method == "POST":
            if not session:
                return self._send(403, {"error": "human session required"})
            try:
                from .launchers import create_pairing_code
                return self._send(200, create_pairing_code(self.server.store.home, session["name"],
                                                            workspace_id))
            except Exception as exc:
                return self._send(500, {"error": str(exc)})
        if path.startswith("/api/launchers/") and path.endswith("/heartbeat") and method == "POST":
            parts = path.strip("/").split("/")
            if len(parts) != 4:
                return self._send(400, {"error": "invalid launcher path"})
            token = ""
            auth = self.headers.get("Authorization", "")
            if auth.startswith("Bearer "):
                token = auth[len("Bearer "):].strip()
            from .launchers import heartbeat
            if not token or not heartbeat(self.server.store.home, parts[2], token):
                return self._send(401, {"error": "invalid launcher token"})
            return self._send(200, {"ok": True})
        if path.startswith("/api/launchers/") and method == "DELETE":
            if not session:
                return self._send(403, {"error": "human session required"})
            from .launchers import revoke
            launcher_id = path.removeprefix("/api/launchers/").strip()
            owner = None if session.get("role") == "admin" else session["name"]
            if not launcher_id or not revoke(self.server.store.home, launcher_id, owner):
                return self._send(404, {"error": "launcher not found"})
            return self._send(200, {"ok": True})

        if path == "/api/launchers" and method == "GET":
            if not session:
                return self._send(403, {"error": "human session required"})
            from .launchers import list_launchers
            return self._send(200, {"launchers": list_launchers(
                self.server.store.home, None, workspace_id)})
        if path == "/api/launcher/tasks" and method == "GET":
            from .launchers import authenticate
            token = self.headers.get("Authorization", "")
            token = token[len("Bearer "):].strip() if token.startswith("Bearer ") else ""
            launcher_id = authenticate(self.server.store.home, token)
            if not launcher_id:
                return self._send(401, {"error": "invalid launcher token"})
            return self._send(200, {"tasks": self.server.issues.launcher_tasks(launcher_id)})
        if path.startswith("/api/launcher/tasks/") and method == "POST":
            from .launchers import authenticate
            token = self.headers.get("Authorization", "")
            token = token[len("Bearer "):].strip() if token.startswith("Bearer ") else ""
            launcher_id = authenticate(self.server.store.home, token)
            if not launcher_id:
                return self._send(401, {"error": "invalid launcher token"})
            parts = path.strip("/").split("/")
            if len(parts) != 5 or parts[4] not in ("claim", "complete", "fail", "heartbeat", "release"):
                return self._send(405, {"error": "unknown launcher action"})
            issue_id = parts[3]
            try:
                data = json.loads(body or b"{}")
                if parts[4] == "claim":
                    issue, claim_token = self.server.issues.launcher_claim(issue_id, launcher_id)
                    return self._send(200, {"issue": issue, "claim_token": claim_token})
                if parts[4] == "heartbeat":
                    return self._send(200, {"ok": self.server.issues.launcher_heartbeat(
                        issue_id, launcher_id, data.get("claim_token", ""))})
                if parts[4] == "release":
                    return self._send(200, self.server.issues.launcher_release(
                        issue_id, launcher_id, data.get("claim_token", "")))
                if parts[4] == "complete":
                    return self._send(200, self.server.issues.launcher_complete(
                        issue_id, launcher_id, data.get("claim_token", ""), data.get("summary", ""),
                        data.get("artifacts") or [], data.get("checks") or [], data.get("usage")))
                return self._send(200, self.server.issues.launcher_fail(
                    issue_id, launcher_id, data.get("claim_token", ""), data.get("error", "launcher failed"),
                    retry=bool(data.get("retry", True))))
            except (PolicyError, ValueError, TypeError) as exc:
                return self._send(400, {"error": str(exc)})

        if path == "/api/launcher" and method == "GET":
            if not session:
                return self._send(403, {"error": "human session required"})
            from .launcher_client import status
            return self._send(200, status(self.server.store.home))
        if path == "/api/launcher/cloud-tasks/execute" and method == "POST":
            if not session:
                return self._send(403, {"error": "human session required"})
            try:
                data = json.loads(body or b"{}")
                from .launcher_client import worker_once
                result = worker_once(self.server.store.home, data.get("issue_id"))
                return self._send(200, {"result": result})
            except Exception as exc:
                return self._send(400, {"error": str(exc)})

        if path == "/api/launcher/login" and method == "POST":
            if not session:
                return self._send(403, {"error": "human session required"})
            try:
                data = json.loads(body or b"{}")
                from .launcher_client import login
                return self._send(200, login(self.server.store.home, data.get("cloud_url", ""),
                                             data.get("username", ""), data.get("password", ""),
                                             data.get("name", "Hengjian Launcher"),
                                             data.get("host", ""), data.get("version", "")))
            except Exception as exc:
                return self._send(400, {"error": str(exc)})

        if path == "/api/launcher/cloud-tasks" and method == "GET":
            if not session:
                return self._send(403, {"error": "human session required"})
            try:
                from .launcher_client import tasks
                return self._send(200, {"tasks": tasks(self.server.store.home)})
            except Exception as exc:
                return self._send(400, {"error": str(exc)})

        if path == "/api/launcher/pair" and method == "POST":
            if not session:
                return self._send(403, {"error": "human session required"})
            try:
                data = json.loads(body or b"{}")
                from .launcher_client import pair
                return self._send(200, pair(self.server.store.home, data.get("cloud_url", ""),
                                            data.get("code", ""), data.get("name", "Hengjian Launcher"),
                                            data.get("host", ""), data.get("version", "")))
            except Exception as exc:
                return self._send(400, {"error": str(exc)})
        if path == "/api/launcher/disconnect" and method == "POST":
            if not session:
                return self._send(403, {"error": "human session required"})
            from .launcher_client import disconnect
            return self._send(200, disconnect(self.server.store.home))

        if path == ME_PATH:
            if session:
                return self._send(200, {"auth": "session", **session})
            status, payload = self.server.api.handle(method, "/api/health", dict(self.headers), b"")
            if status == 200:
                return self._send(200, {"auth": "token"})
            return self._send(401, {"error": "unauthorized"})

        if path == LOGOUT_PATH and method == "POST":
            if not session:
                return self._send(401, {"error": "unauthorized"})
            users.logout(raw)
            return self._send(200, {"ok": True}, headers=self._clear_session_cookie_header())

        if path == "/api/pricing":
            session = getattr(self, "_session", None)
            if not session or session.get("role") != "admin":
                return self._send(403 if session else 401, {"error": "admin session required"})
            from .pricing import load as load_pricing, save as save_pricing
            if method == "GET":
                return self._send(200, load_pricing(self.server.store.home))
            if method == "PUT":
                try:
                    data = json.loads(body or b"{}")
                    return self._send(200, save_pricing(self.server.store.home, data))
                except (ValueError, UnicodeError) as exc:
                    return self._send(400, {"error": str(exc) or "invalid JSON body"})
            return self._send(405, {"error": "method not allowed"})

        if path == "/api/issue-policy":
            session = getattr(self, "_session", None)
            if not session or session.get("role") != "admin":
                return self._send(403 if session else 401, {"error": "admin session required"})
            from .issue_policy import load as load_policy, save as save_policy
            if method == "GET":
                return self._send(200, load_policy(self.server.store.home))
            if method == "PUT":
                try:
                    data = json.loads(body or b"{}")
                    if not isinstance(data, dict):
                        raise ValueError
                    return self._send(200, save_policy(self.server.store.home, data))
                except (ValueError, UnicodeError):
                    return self._send(400, {"error": "invalid JSON body"})
            return self._send(405, {"error": "method not allowed"})

        if path == "/api/alerts":
            session = getattr(self, "_session", None)
            if not session:
                status, _ = self.server.api.handle("GET", "/api/health", dict(self.headers), b"")
                if status != 200:
                    return self._send(401, {"error": "unauthorized"})
            try:
                from .alerts import list_alerts
                query = parse_qs(urlsplit(self.path).query)
                include = (query.get("all") or ["0"])[0] == "1"
                return self._send(200, {"alerts": list_alerts(self.server.store.home, include)})
            except Exception:
                return self._send(503, {"error": "alerts unavailable"})
        if path.startswith("/api/alerts/") and path.endswith("/ack") and method == "POST":
            session = getattr(self, "_session", None)
            if not session:
                return self._send(403, {"error": "human session required"})
            parts = path.strip("/").split("/")
            if len(parts) != 4 or not parts[2].isdigit():
                return self._send(400, {"error": "invalid alert id"})
            from .alerts import acknowledge
            if not acknowledge(self.server.store.home, parts[2]):
                return self._send(404, {"error": "alert not found"})
            return self._send(200, {"ok": True})

        # Durable Issue queue: agent tokens may create/claim/comment; human
        # review and approval require a real session.
        if path == "/api/issues" or path.startswith("/api/issues/"):
            data = {}
            if body:
                try:
                    data = json.loads(body)
                    if not isinstance(data, dict):
                        raise ValueError
                except (ValueError, UnicodeError):
                    return self._send(400, {"error": "invalid JSON body"})
            session = getattr(self, "_session", None)
            if not session:
                status, _ = self.server.api.handle("GET", "/api/health", dict(self.headers), b"")
                if status != 200:
                    return self._send(401, {"error": "unauthorized"})
            actor = str(session.get("name")) if session else "dsh"
            parts = path.strip("/").split("/")
            try:
                if len(parts) == 2 and method == "GET":
                    query = parse_qs(urlsplit(self.path).query)
                    state = (query.get("state") or [None])[0]
                    return self._send(200, {"issues": self.server.issues.list(state, workspace_id)})
                if len(parts) == 2 and method == "POST":
                    issue = self.server.issues.create(
                        title=data.get("title"), assignee=data.get("assignee"),
                        body=data.get("body", ""), parent_id=data.get("parent_id"),
                        blockers=data.get("blockers") or [], reviewer=data.get("reviewer"),
                        approver=data.get("approver"), priority=data.get("priority", 0),
                        acceptance=data.get("acceptance") or [], checks=data.get("checks") or [],
                        model=data.get("model"), max_attempts=data.get("max_attempts", 3),
                        max_tokens=data.get("max_tokens", 100000),
                        max_calls=data.get("max_calls", 4),
                        max_cost_usd=data.get("max_cost_usd"),
                        launcher_id=data.get("launcher_id"), workspace_id=workspace_id, actor=actor)
                    return self._send(201, issue)
                if len(parts) < 3:
                    return self._send(405, {"error": "method not allowed"})
                issue_id = parts[2]
                issue = self.server.issues.get(issue_id)
                if issue.get("workspace_id") != workspace_id:
                    return self._send(404, {"error": "issue not found"})
                if len(parts) == 3 and method == "GET":
                    return self._send(200, issue)
                if len(parts) != 4 or method != "POST":
                    return self._send(405, {"error": "method not allowed"})
                action = parts[3]
                if action in ("review", "approve") and not session:
                    return self._send(403, {"error": "human session required"})
                if action == "comment":
                    return self._send(200, self.server.issues.comment(
                        issue_id, actor, data.get("body", "")))
                if action == "claim":
                    return self._send(200, self.server.issues.claim(
                        issue_id, data.get("worker") or actor,
                        int(data.get("lease_seconds") or 60)))
                if action == "heartbeat":
                    return self._send(200, self.server.issues.heartbeat(
                        issue_id, data.get("claim_token", ""),
                        int(data.get("lease_seconds") or 60)))
                if action == "complete":
                    return self._send(200, self.server.issues.complete(
                        issue_id, data.get("claim_token", ""), data.get("summary", ""),
                        data.get("artifacts") or [], data.get("checks") or [], data.get("usage")))
                if action == "block":
                    return self._send(200, self.server.issues.block(
                        issue_id, data.get("blocker_id"), actor))
                if action == "unblock":
                    return self._send(200, self.server.issues.unblock(
                        issue_id, data.get("blocker_id"), actor))
                if action == "review":
                    return self._send(200, self.server.issues.review(
                        issue_id, actor, data.get("verdict"), data.get("comment", ""),
                        admin=bool(session and session.get("role") == "admin")))
                if action == "approve":
                    return self._send(200, self.server.issues.approve(
                        issue_id, actor, admin=bool(session and session.get("role") == "admin")))
                if action == "retry":
                    return self._send(200, self.server.issues.retry(issue_id, actor))
                if action == "cancel":
                    return self._send(200, self.server.issues.cancel(
                        issue_id, actor, data.get("reason", "")))
                return self._send(405, {"error": "unknown issue action"})
            except (PolicyError, ValueError, TypeError) as exc:
                return self._send(400, {"error": str(exc)})
            except Exception:
                return self._send(503, {"error": "issue service unavailable"})

        # Read-only registry inventory: a machine token or an admin session may
        # read it. Mutating install/bind operations remain admin-only below.
        if path == "/api/agents/inventory" and method == "GET":
            session_ok = bool(getattr(self, "_session", None))
            status, _ = self.server.api.handle("GET", "/api/health", dict(self.headers), b"")
            if not session_ok and status != 200:
                return self._send(401, {"error": "unauthorized"})
            try:
                from .agent_inventory import inventory_payload
                return self._send(200, inventory_payload(self.server.store.home))
            except Exception:
                return self._send(503, {"error": "agent inventory unavailable"})

        if path.startswith("/api/agents/") and path.endswith("/runs") and method == "GET":
            session = getattr(self, "_session", None)
            if not session:
                status, _ = self.server.api.handle("GET", "/api/health", dict(self.headers), b"")
                if status != 200:
                    return self._send(401, {"error": "unauthorized"})
            parts = path.strip("/").split("/")
            if len(parts) != 4:
                return self._send(400, {"error": "invalid agent path"})
            try:
                from .agent_workspace import runs
                query = parse_qs(urlsplit(self.path).query)
                limit = max(1, min(50, int((query.get("limit") or ["20"])[0])))
                return self._send(200, {"agent": parts[2], "runs": runs(self.server.store.home, parts[2], limit)})
            except (ValueError, TypeError):
                return self._send(400, {"error": "invalid agent or limit"})

        if path.startswith("/api/agents/") and "/lifecycle" in path:
            session = getattr(self, "_session", None)
            if not session or session.get("role") != "admin":
                return self._send(401 if not session else 403, {"error": "admin session required"})
            parts = path.strip("/").split("/")
            if len(parts) < 4:
                return self._send(400, {"error": "invalid lifecycle path"})
            agent = parts[2]
            try:
                from .agent_lifecycle import load_config, restart, save_config, start, status, stop
                if len(parts) == 4 and method == "GET":
                    return self._send(200, {"config": load_config(self.server.store.home, agent),
                                            "status": status(self.server.store.home, agent)})
                if len(parts) == 4 and method == "PUT":
                    data = json.loads(body or b"{}")
                    if not isinstance(data, dict):
                        raise PolicyError("body must be an object")
                    return self._send(200, save_config(self.server.store.home, agent, data))
                if len(parts) == 5 and method == "POST" and parts[3] in ("start", "stop", "restart"):
                    action = {"start": start, "stop": stop, "restart": restart}[parts[3]]
                    return self._send(200, action(self.server.store.home, agent))
                return self._send(405, {"error": "method not allowed"})
            except (ValueError, TypeError) as exc:
                return self._send(400, {"error": str(exc)})
            except Exception as exc:
                return self._send(400, {"error": str(exc)})

        # Installation and presets require a real admin session, never the
        # delegated/machine credential used for the general-purpose API.
        if path == "/api/agents" or path.startswith("/api/agents/"):
            try:
                status, payload = self.server.agent_installer.handle(method, path, body, session)
            except Exception:
                status, payload = 503, {"error": "agent management unavailable"}
            return self._send(status, payload)

        if path == "/api/workspaces/invite" and method == "POST":
            session = getattr(self, "_session", None)
            if not session:
                return self._send(401, {"error": "human session required"})
            from .workspaces import membership as workspace_membership
            if session.get("role") != "admin" and workspace_membership(
                    self.server.store.home, workspace_id, session["name"]) not in ("owner", "admin"):
                return self._send(403, {"error": "workspace owner/admin required"})
            try:
                from .workspaces import create_invite
                return self._send(200, create_invite(self.server.store.home,
                                                     workspace_id, session["name"]))
            except Exception as exc:
                return self._send(400, {"error": str(exc)})
        if path == "/api/workspaces/invites" and method == "GET":
            session = getattr(self, "_session", None)
            if not session or session.get("role") != "admin":
                return self._send(403 if session else 401, {"error": "admin session required"})
            from .workspaces import active_invites
            return self._send(200, {"invites": active_invites(self.server.store.home, workspace_id)})

        if path == "/api/workspaces" and method == "GET":
            if not session:
                return self._send(403, {"error": "human session required"})
            from .workspaces import user_workspaces
            return self._send(200, {"workspaces": user_workspaces(
                self.server.store.home, session["name"], workspace_id)})
        if path == "/api/workspace/switch" and method == "POST":
            if not session:
                return self._send(403, {"error": "human session required"})
            try:
                data = json.loads(body or b"{}")
                target = str(data.get("workspace") or "")
                from .workspaces import membership
                if not membership(self.server.store.home, target, session["name"]):
                    return self._send(403, {"error": "not a member of this workspace"})
                self.server.users.switch_workspace(self._session_token, target)
                return self._send(200, {"ok": True, "workspace": target})
            except Exception as exc:
                return self._send(400, {"error": str(exc)})
        if path == "/api/workspace/join" and method == "POST":
            if not session:
                return self._send(403, {"error": "human session required"})
            try:
                data = json.loads(body or b"{}")
                from .workspaces import add_member, consume
                target = consume(self.server.store.home, data.get("invite", ""))
                add_member(self.server.store.home, target, session["name"], "member")
                self.server.users.switch_workspace(self._session_token, target)
                return self._send(200, {"ok": True, "workspace": target})
            except Exception as exc:
                return self._send(400, {"error": str(exc)})

        if path == "/api/workspace/leave" and method == "POST":
            if not session:
                return self._send(403, {"error": "human session required"})
            try:
                from .workspaces import get, leave, membership, user_workspaces
                if membership(self.server.store.home, workspace_id, session["name"]) != "owner":
                    remaining = [item for item in user_workspaces(
                        self.server.store.home, session["name"], workspace_id)
                        if item["id"] != workspace_id]
                    if not remaining:
                        return self._send(409, {"error": "join another workspace before leaving the last one"})
                    leave(self.server.store.home, workspace_id, session["name"])
                    next_workspace = remaining[0]["id"]
                    self.server.users.set_primary_workspace(session["name"], next_workspace)
                    self.server.users.switch_workspace(self._session_token, next_workspace)
                    return self._send(200, {"ok": True, "workspace": next_workspace})
                return self._send(409, {"error": "owner cannot leave; transfer ownership first"})
            except Exception as exc:
                return self._send(400, {"error": str(exc)})
        if path == "/api/workspace/transfer" and method == "POST":
            if not session:
                return self._send(403, {"error": "human session required"})
            try:
                data = json.loads(body or b"{}")
                from .workspaces import transfer_owner
                meta = transfer_owner(self.server.store.home, workspace_id, session["name"],
                                      str(data.get("member") or ""))
                return self._send(200, {"workspace": meta})
            except Exception as exc:
                return self._send(400, {"error": str(exc)})
        if path.startswith("/api/workspace/members/") and path.endswith("/role") and method == "POST":
            if not session:
                return self._send(403, {"error": "human session required"})
            parts = path.strip("/").split("/")
            if len(parts) != 5:
                return self._send(400, {"error": "invalid member path"})
            try:
                data = json.loads(body or b"{}")
                from .workspaces import set_role
                return self._send(200, set_role(self.server.store.home, workspace_id,
                                                session["name"], parts[3], data.get("role", "member")))
            except Exception as exc:
                return self._send(400, {"error": str(exc)})

        if path == "/api/workspace/rename" and method == "POST":
            if not session:
                return self._send(403, {"error": "human session required"})
            try:
                data = json.loads(body or b"{}")
                from .workspaces import rename
                return self._send(200, rename(self.server.store.home, workspace_id,
                                              session["name"], data.get("name", "")))
            except Exception as exc:
                return self._send(400, {"error": str(exc)})

        if path == "/api/workspace" and method == "GET":
            if not session:
                return self._send(403, {"error": "human session required"})
            from .workspaces import ensure, get, members
            ensure(self.server.store.home, workspace_id, session["name"])
            return self._send(200, {"workspace": get(self.server.store.home, workspace_id),
                                    "members": members(self.server.store.home, workspace_id)["members"]})
        if path == "/api/workspace/members" and method == "GET":
            if not session:
                return self._send(403, {"error": "human session required"})
            from .workspaces import ensure, members
            ensure(self.server.store.home, workspace_id, session["name"])
            return self._send(200, members(self.server.store.home, workspace_id))
        if path == "/api/members":
            session = getattr(self, "_session", None)
            if not session:
                return self._send(401, {"error": "human session required"})
            from .workspaces import membership as workspace_membership
            if session.get("role") != "admin" and workspace_membership(
                    self.server.store.home, workspace_id, session["name"]) not in ("owner", "admin"):
                return self._send(403, {"error": "workspace owner/admin required"})
            users = self.server.users
            if method == "GET":
                return self._send(200, {"members": users.users()})
            if method == "POST":
                try:
                    data = json.loads(body or b"{}")
                    if not isinstance(data, dict):
                        raise ValueError
                    name = str(data.get("username") or data.get("name") or "").strip()
                    password = str(data.get("password") or "")
                    role = str(data.get("role") or "operator")
                    users.add_user(name, password, role, workspace=workspace_id)
                    from .workspaces import add_member, ensure
                    ensure(self.server.store.home, workspace_id, session["name"])
                    add_member(self.server.store.home, workspace_id, name, "member")
                    return self._send(201, {"ok": True, "members": users.users()})
                except Exception as exc:
                    return self._send(400, {"error": str(exc) or "invalid member"})
            return self._send(405, {"error": "method not allowed"})
        if path.startswith("/api/members/") and method == "DELETE":
            session = getattr(self, "_session", None)
            if not session:
                return self._send(401, {"error": "human session required"})
            from .workspaces import membership as workspace_membership
            if session.get("role") != "admin" and workspace_membership(
                    self.server.store.home, workspace_id, session["name"]) not in ("owner", "admin"):
                return self._send(403, {"error": "workspace owner/admin required"})
            name = path.removeprefix("/api/members/").strip()
            if not name or "/" in name:
                return self._send(400, {"error": "invalid member name"})
            if name == session.get("name"):
                return self._send(409, {"error": "cannot remove your own account"})
            users = self.server.users
            target = next((u for u in users.users() if u["name"] == name), None)
            if target is None or target.get("workspace") != workspace_id:
                return self._send(404, {"error": "member not found"})
            from .workspaces import get as get_workspace
            owner = (get_workspace(self.server.store.home, workspace_id) or {}).get("owner")
            if name == owner:
                return self._send(409, {"error": "cannot remove the workspace owner"})
            if target["role"] == "admin" and sum(u["role"] == "admin" for u in users.users()) <= 1:
                return self._send(409, {"error": "cannot remove the last admin"})
            users.remove_user(name)
            return self._send(200, {"ok": True, "members": users.users()})

        if session:
            allowed = ROLE_METHODS.get(session["role"], set())
            if method not in allowed:
                return self._send(403, {"error": "forbidden for role"})
            if path.startswith("/api/users") and session["role"] != "admin":
                return self._send(403, {"error": "admin only"})
            # The session is already authorised; delegate to the API layer with
            # its internal machine credential so the per-request audit still fires.
            status, payload = self.server.api.handle(
                method, path, {"Authorization": f"Bearer {self.server.session_token}"}, body)
            return self._send(status, payload)

        status, payload = self.server.api.handle(method, path, dict(self.headers), body)
        self._send(status, payload)


def launcher_worker_loop(httpd, *, interval: float = 10.0) -> None:
    if os.environ.get("DUTANG_LAUNCHER_EXECUTE") != "1":
        return
    from .launcher_client import worker_once
    while not httpd.launcher_stop.wait(interval):
        try:
            worker_once(httpd.store.home)
        except Exception:
            pass


def launcher_heartbeat_loop(httpd, *, interval: float = 30.0) -> None:
    from .launcher_client import heartbeat
    while not httpd.launcher_stop.wait(interval):
        try:
            heartbeat(httpd.store.home)
        except Exception:
            pass


def worker_loop(httpd, *, interval: float = 2.0, allow_unpriced: bool = False) -> None:
    """Trusted consumer: drain the request queue, never auto-replay running work."""
    from .ai import DecisionClient
    from .role_binding import load

    def client_for(audit):
        instance = httpd.store.home
        if (instance / "roles.json").exists():
            return DecisionClient(audit, bindings=load(instance)["bindings"], instance=instance)
        return DecisionClient(audit)

    previous_error, failures = None, 0
    while not getattr(httpd, "stop_worker", False):
        error = None
        try:
            result = httpd.api.process_next(
                client_for, allow_unpriced=allow_unpriced)
            error = result.get("error") if result else None
        except Exception as exc:
            # Per-run failures are persisted inside process_next while it holds
            # the consumer lock. Errors outside a claimed run have no run owner.
            error = worker_error_summary(exc)
            result = None
        if error:
            kind = error["error_type"]
            failures = failures + 1 if kind == previous_error else 1
            previous_error = kind
            if result is None and failures == 1:
                logger.error("worker unavailable: %s", kind)
            if failures % 3 == 0:
                logger.error("worker failing repeatedly: %s", kind)
        elif result is not None:
            previous_error, failures = None, 0
        if result is None:
            threading.Event().wait(interval)


def issue_recovery_loop(httpd, *, interval: float = 15.0) -> None:
    """Requeue Issue claims whose lease expired; never executes a model here."""
    while not httpd.issue_stop.wait(interval):
        try:
            httpd.issues.recover_expired()
        except Exception as exc:
            logger.error("issue lease recovery failed: %s", type(exc).__name__)


def issue_heartbeat_loop(httpd, issue_id, token, stop):
    """Renew an Issue lease while a model call is running (no double claim)."""
    while not stop.wait(30):
        try:
            httpd.issues.heartbeat(issue_id, token, lease_seconds=120)
        except Exception:
            return


def issue_worker_loop(httpd, *, interval: float = 2.0, worker_index: int = 0) -> None:
    """Consume wakeups, claim queued Issues, execute one bounded agent round."""
    from .issue_executor import execute_issue
    worker = f"issue-worker:{os.getpid()}:{worker_index}"
    actionable = {"assigned", "commented", "unblocked", "recovered", "changes_requested"}
    while not httpd.issue_stop.wait(interval):
        try:
            httpd.issues.recover_expired()
            wake = httpd.issues.claim_wakeup(worker)
            if wake is None:
                httpd.issues.wake_due_retries()
                continue
        except Exception as exc:
            logger.error("issue wakeup scan failed: %s", type(exc).__name__)
            continue
        try:
            issue = httpd.issues.get(wake["issue_id"])
            if issue["state"] == "queued" and wake["kind"] in actionable:
                try:
                    claimed = httpd.issues.claim(issue["id"], worker, lease_seconds=120)
                except PolicyError:
                    claimed = None
                if claimed is not None and claimed.get("claim_token"):
                    token = claimed.get("claim_token")
                    stop = threading.Event()
                    beat = threading.Thread(target=issue_heartbeat_loop,
                                            args=(httpd, issue["id"], token, stop), daemon=True)
                    beat.start()
                    try:
                        result = execute_issue(claimed, httpd.store.home)
                    except Exception as exc:
                        stop.set(); beat.join(timeout=2)
                        logger.error("issue execution failed: %s", type(exc).__name__)
                        try:
                            httpd.issues.fail(issue["id"], token, f"{type(exc).__name__}: {exc}")
                        except Exception:
                            pass
                        continue
                    stop.set(); beat.join(timeout=2)
                    try:
                        if result["status"] == "delegate":
                            httpd.issues.delegate(issue["id"], token, result["subtasks"])
                        elif result["status"] == "blocked":
                            httpd.issues.fail(issue["id"], token, result["blocker"],
                                              retry=bool(result.get("retryable", True)))
                        else:
                            httpd.issues.complete(issue["id"], token, result["summary"],
                                                  result["artifacts"], result.get("checks") or [], result.get("usage"))
                    except Exception as exc:
                        logger.error("issue settlement failed: %s", type(exc).__name__)
        finally:
            try:
                httpd.issues.finish_wakeup(wake["id"])
            except Exception:
                pass


def lifecycle_supervisor_loop(httpd, *, interval: float = 15.0) -> None:
    """Restart opted-in daemons; never starts an unconfigured CLI agent."""
    from .agent_lifecycle import supervise
    while not httpd.lifecycle_stop.wait(interval):
        try:
            supervise(httpd.store.home)
        except Exception as exc:
            logger.error("agent lifecycle supervisor failed: %s", type(exc).__name__)


def sync_loop(httpd, *, interval: float = 2.0) -> None:
    """Incremental read-only projection; idempotent by mutation id."""
    while not getattr(httpd, "sync_stop", None) or not httpd.sync_stop.is_set():
        try:
            project_store(httpd.store, httpd.sync_journal)
        except Exception as exc:
            logger.error("sync projection failed: %s", type(exc).__name__)
        if httpd.sync_stop.wait(interval):
            return


class LocalHTTPServer(ThreadingHTTPServer):
    """HTTPServer without socket.getfqdn(), which can block on macOS DNS."""

    def server_bind(self):
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = str(host)
        self.server_port = int(port)


def serve(home: Path, *, host: str = "127.0.0.1", port: int = 0,
          board_key: Path | None = None, verbose: bool = False,
          worker: bool = False, allow_unpriced: bool = False,
          sync: bool = False, discovery_file: Path | None = None,
          issue_worker: bool = False, issue_workers: int = 1) -> ThreadingHTTPServer:
    store = Store(home, board_key) if board_key else Store(home)
    # 启动期一致性回收：断电/被杀后遗留的 running 队列项与租约（幂等、只在无消费进程时动手）
    try:
        from .recovery import reconcile
        outcome = reconcile(store)
        reclaimed = (outcome.get("queue") or {}).get("reclaimed") or []
        if reclaimed:
            logger.warning("reclaimed %d orphaned submission(s) at startup: %s",
                           len(reclaimed), ", ".join(str(x) for x in reclaimed))
    except Exception as exc:  # 回收失败不应阻止服务启动
        logger.error("startup reconciliation failed: %s", type(exc).__name__)
    httpd = LocalHTTPServer((host, port), Handler)
    httpd.api = API(store)
    httpd.store = store
    httpd.users = UserStore(store.home / "auth")
    httpd.onboarding_lock = threading.Lock()
    from .agent_install import Installer
    httpd.agent_installer = Installer(store.home)
    httpd.model_access = ModelAccess(store.home)
    httpd.issues = IssueStore(store)
    httpd.issue_stop = threading.Event()
    httpd.launcher_stop = threading.Event()
    httpd.launcher_thread = threading.Thread(
        target=launcher_heartbeat_loop, args=(httpd,), daemon=True)
    httpd.launcher_thread.start()
    httpd.launcher_worker_thread = threading.Thread(
        target=launcher_worker_loop, args=(httpd,), daemon=True)
    httpd.launcher_worker_thread.start()
    httpd.lifecycle_stop = threading.Event()
    httpd.lifecycle_supervisor = threading.Thread(
        target=lifecycle_supervisor_loop, args=(httpd,), daemon=True)
    httpd.lifecycle_supervisor.start()
    httpd.issue_recovery = threading.Thread(
        target=issue_recovery_loop, args=(httpd,), daemon=True)
    httpd.issue_recovery.start()
    httpd.issue_runners = []
    worker_count = max(1, min(4, int(issue_workers or 1)))
    if issue_worker or worker_count > 1:
        for index in range(worker_count):
            runner = threading.Thread(target=issue_worker_loop, args=(httpd,),
                                      kwargs={"worker_index": index}, daemon=True)
            runner.start()
            httpd.issue_runners.append(runner)
    httpd.sync_journal = SyncJournal(store.home / "sync-journal.sqlite3")
    httpd.sync_stop = threading.Event()
    httpd.sync_runner = None
    if sync:
        httpd.sync_runner = threading.Thread(target=sync_loop, args=(httpd,), daemon=True)
        httpd.sync_runner.start()
    # Process-local random token; never persisted. It only authorises requests
    # that already passed session auth in this process.
    session_token = secrets.token_urlsafe(32)
    httpd.api.add_ephemeral_token("session-delegated", session_token)
    httpd.session_token = session_token
    local_token = secrets.token_urlsafe(32)
    httpd.api.add_ephemeral_token("local-client", local_token)
    httpd.discovery_token = local_token
    if discovery_file is not None:
        discovery_file = Path(discovery_file)
        discovery_file.parent.mkdir(parents=True, exist_ok=True)
        atomic_json_write(discovery_file, {
            "version": 1,
            "port": httpd.server_port,
            "token": local_token,
            "pid": os.getpid(),
            "home": str(store.home),
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        })
        discovery_file.chmod(0o600)
    httpd.runner = None
    if worker:
        # Pipeline admission always supplies estimated_cents=None, including on
        # a fresh store. Installed board policy remains authoritative.
        from .budget import policy
        with store.connect() as conn:
            unpriced_disabled = policy(conn)["unpriced"] == "block"
        if not allow_unpriced and unpriced_disabled:
            logger.warning("worker running with unpriced usage disabled: queued runs will fail")
        import threading as _threading
        httpd.runner = _threading.Thread(target=worker_loop, args=(httpd,),
                                         kwargs={"allow_unpriced": allow_unpriced},
                                         daemon=True)
        httpd.runner.start()
    httpd.verbose = verbose
    return httpd


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Fusion web server (loopback by default)")
    p.add_argument("--home", type=Path, required=True)
    p.add_argument("--board-key-file", type=Path)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8787)
    p.add_argument("--verbose", action="store_true")
    p.add_argument("--worker", action="store_true",
                   help="also drain the request queue (needs the provider key in env)")
    p.add_argument("--allow-unpriced", action="store_true",
                   help="allow model usage whose price is unknown (recorded as unknown)")
    p.add_argument("--sync", action="store_true",
                   help="incrementally project tasks/events into the local sync journal")
    p.add_argument("--discovery-file", type=Path, default=None,
                   help="write port/token discovery JSON (0600) for local clients")
    p.add_argument("--issue-worker", action="store_true",
                   help="claim and execute queued Issues with the bound agent")
    p.add_argument("--issue-workers", type=int, default=1,
                   help="number of concurrent Issue workers (1-4)")
    a = p.parse_args(argv)
    httpd = serve(a.home, host=a.host, port=a.port, board_key=a.board_key_file,
                  verbose=a.verbose, worker=a.worker, allow_unpriced=a.allow_unpriced,
                  sync=a.sync, discovery_file=a.discovery_file,
                  issue_worker=a.issue_worker, issue_workers=a.issue_workers)
    print(f"fusion server: http://{a.host}:{httpd.server_port}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
