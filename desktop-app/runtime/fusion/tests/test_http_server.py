"""Server wiring tests: real HTTP against a loopback ThreadingHTTPServer."""
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


def call(base, path, token=None, method="GET"):
    req = urllib.request.Request(base + path, method=method)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def test_health_is_public_and_api_requires_token(http):
    httpd, base = http
    assert call(base, "/api/health")[0] == 200
    assert call(base, "/api/tasks")[0] == 401
    assert call(base, "/api/ai-runs")[0] == 401


def test_valid_token_reaches_api_and_timeline(http):
    httpd, base = http
    token = "a" * 8 + "b7f3c9d1e2a4" * 3 + "f0e1d2c3b4a5"
    httpd.api.add_token("t", token)
    assert call(base, "/api/tasks", token) == (200, {"tasks": []})
    assert call(base, "/api/ai-runs", token) == (200, {"runs": []})


def test_unknown_paths_are_404_and_writes_stay_closed(http):
    httpd, base = http
    token = "a" * 8 + "b7f3c9d1e2a4" * 3 + "f0e1d2c3b4a5"
    httpd.api.add_token("t", token)
    assert call(base, "/api/nope", token)[0] == 404
    assert call(base, "/api/tasks", method="POST")[0] in (401, 404, 405)


def test_audit_line_written_for_unauthenticated_attempt(http):
    httpd, base = http
    call(base, "/api/tasks")
    audit = httpd.store.home / "api-audit.jsonl"
    assert audit.exists()
    first = json.loads(audit.read_text(encoding="utf-8").splitlines()[0])
    assert first["status"] == 401


def test_model_providers_route_is_mounted_and_records_stay_private(http):
    httpd, base = http
    status, payload = call(base, "/api/models/providers")
    assert status == 200 and payload.get("providers")
    assert call(base, "/api/models")[0] == 401
