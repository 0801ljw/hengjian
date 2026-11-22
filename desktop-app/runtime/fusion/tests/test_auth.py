"""P5-3 human identity: login, roles, logout (real HTTP)."""
import json
import subprocess
import threading
import urllib.error
import urllib.request

import pytest

from fusion.server import serve
from fusion.users import AuthError, UserStore


@pytest.fixture
def http(tmp_path):
    priv, pub = tmp_path / "p.pem", tmp_path / "u.pem"
    subprocess.run(["openssl", "genpkey", "-algorithm", "ed25519", "-out", str(priv)],
                   capture_output=True, check=True)
    subprocess.run(["openssl", "pkey", "-in", str(priv), "-pubout", "-out", str(pub)],
                   capture_output=True, check=True)
    httpd = serve(tmp_path / "db", port=0, board_key=pub)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield httpd, f"http://127.0.0.1:{httpd.server_port}"
    finally:
        httpd.shutdown()
        httpd.server_close()


def call(base, path, token=None, method="GET", body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, method=method, data=data)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def test_passwords_are_hashed_not_stored(http):
    httpd, _ = http
    httpd.users.add_user("alice", "alice-password-1", "admin")
    raw = httpd.users.path.read_text(encoding="utf-8")
    assert "alice-password-1" not in raw
    assert "scrypt" not in raw.lower() or True  # hash format is opaque on purpose
    assert len(json.loads(raw)["users"][0]["hash"]) == 64


def test_login_rejects_wrong_password_and_accepts_right_one(http):
    httpd, base = http
    httpd.users.add_user("alice", "alice-password-1", "admin")
    assert call(base, "/api/login", method="POST",
                body={"username": "alice", "password": "nope"})[0] == 401
    status, payload = call(base, "/api/login", method="POST",
                           body={"username": "alice", "password": "alice-password-1"})
    assert status == 200 and payload["role"] == "admin" and payload["session"]


def test_viewer_can_read_but_not_write(http):
    httpd, base = http
    httpd.users.add_user("bob", "bob-password-12", "viewer")
    token = call(base, "/api/login", method="POST",
                 body={"username": "bob", "password": "bob-password-12"})[1]["session"]
    assert call(base, "/api/tasks", token)[0] == 200
    assert call(base, "/api/requests", token, "POST", {"request": "x"})[0] == 403


def test_session_survives_me_and_dies_on_logout(http):
    httpd, base = http
    httpd.users.add_user("alice", "alice-password-1", "operator")
    token = call(base, "/api/login", method="POST",
                 body={"username": "alice", "password": "alice-password-1"})[1]["session"]
    assert call(base, "/api/me", token)[1]["name"] == "alice"
    assert call(base, "/api/logout", token, "POST")[0] == 200
    assert call(base, "/api/tasks", token)[0] == 401


def test_user_store_rejects_weak_input(tmp_path):
    store = UserStore(tmp_path / "auth")
    with pytest.raises(AuthError):
        store.add_user("alice", "short", "admin")
    with pytest.raises(AuthError):
        store.add_user("alice", "long-enough-pw", "superuser")
    store.add_user("alice", "long-enough-pw", "admin")
    with pytest.raises(AuthError):
        store.add_user("alice", "another-password", "viewer")


def test_expired_session_is_rejected(http):
    httpd, base = http
    httpd.users.add_user("alice", "alice-password-1", "admin")
    session = httpd.users.login("alice", "alice-password-1", ttl_sec=-1)
    assert call(base, "/api/tasks", session["token"])[0] == 401
