"""Item 1 的行为测试：httpOnly cookie 会话 + CSRF 保护（真 HTTP）。

覆盖：
1. 登录/注册下发 HttpOnly + SameSite=Strict 的 Set-Cookie
2. 只带 cookie（不带 Authorization）可读 /api/me、/api/tasks
3. cookie 会话做状态变更时，缺 X-Fusion-Request 头 → 403
4. 带上该头 → 放行
5. 登出清 cookie，之后旧 cookie 失效
6. Bearer（机器/CLI）路径不受 CSRF 限制，保持可用
"""
import http.cookiejar
import json
import subprocess
import threading
import urllib.error
import urllib.request

import pytest

from fusion.server import serve


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


def raw_call(base, path, method="GET", body=None, headers=None, cookie=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, method=method, data=data)
    req.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    if cookie:
        req.add_header("Cookie", cookie)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read().decode()), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode()), dict(e.headers)


def register(base, username="alice", password="alice-password-1"):
    return raw_call(base, "/api/register", "POST", {"username": username, "password": password})


def session_cookie(headers):
    for key, value in headers.items():
        if key.lower() == "set-cookie" and value.startswith("fusion_session="):
            return value.split(";", 1)[0]
    return ""


def test_login_sets_httponly_strict_cookie(http):
    _, base = http
    status, payload, headers = register(base)
    assert status == 200 and payload["session"]
    cookie = headers.get("Set-Cookie", "")
    assert cookie.startswith("fusion_session="), cookie
    lowered = cookie.lower()
    assert "httponly" in lowered, cookie
    assert "samesite=strict" in lowered, cookie
    assert "path=/" in lowered, cookie


def test_cookie_alone_authenticates_reads(http):
    _, base = http
    status, _, headers = register(base)
    assert status == 200
    cookie = session_cookie(headers)
    assert cookie

    status, payload, _ = raw_call(base, "/api/me", cookie=cookie)
    assert status == 200 and payload["auth"] == "session"

    status, payload, _ = raw_call(base, "/api/tasks", cookie=cookie)
    assert status == 200


def test_cookie_write_requires_csrf_header(http):
    _, base = http
    status, _, headers = register(base)
    assert status == 200
    cookie = session_cookie(headers)
    assert cookie

    # 缺自定义头 → 403（跨站伪造请求正是这种形状）
    status, payload, _ = raw_call(base, "/api/requests", "POST",
                                  {"request": "伪造的跨站请求"}, cookie=cookie)
    assert status == 403
    assert "X-Fusion-Request" in payload.get("error", "")

    # 带上头 → 放行
    status, payload, _ = raw_call(base, "/api/requests", "POST",
                                  {"request": "正常提交"}, cookie=cookie,
                                  headers={"X-Fusion-Request": "1"})
    assert status == 200, payload


def test_logout_clears_cookie_and_session(http):
    _, base = http
    status, _, headers = register(base)
    cookie = session_cookie(headers)
    assert status == 200 and cookie

    status, _, headers = raw_call(base, "/api/logout", "POST", {},
                                  cookie=cookie, headers={"X-Fusion-Request": "1"})
    assert status == 200
    cleared = headers.get("Set-Cookie", "")
    assert "Max-Age=0" in cleared or "max-age=0" in cleared.lower()

    # 旧 cookie 已经无效
    status, _, _ = raw_call(base, "/api/me", cookie=cookie)
    assert status == 401


def test_bearer_path_still_works_without_csrf(http):
    _, base = http
    status, payload, _ = register(base)
    assert status == 200
    token = payload["session"]

    # 机器/CLI 用 Bearer：读 + 写都不需要 CSRF 头
    status, _, _ = raw_call(base, "/api/me", headers={"Authorization": "Bearer " + token})
    assert status == 200
    status, _, _ = raw_call(base, "/api/requests", "POST", {"request": "机器提交"},
                            headers={"Authorization": "Bearer " + token})
    assert status == 200
