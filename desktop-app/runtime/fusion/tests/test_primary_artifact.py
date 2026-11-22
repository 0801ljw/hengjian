"""Primary-artifact attribution and the awaiting_board guidance payload."""
import json
import subprocess
import threading

import pytest

from fusion.server import list_artifacts, serve


def _store(tmp_path):
    from fusion.core import Store
    priv, pub = tmp_path / "p.pem", tmp_path / "u.pem"
    subprocess.run(["openssl", "genpkey", "-algorithm", "ed25519", "-out", str(priv)], capture_output=True, check=True)
    subprocess.run(["openssl", "pkey", "-in", str(priv), "-pubout", "-out", str(pub)], capture_output=True, check=True)
    return Store(tmp_path / "db", pub)


def _manifest(store, files, primary_subtask=None):
    run = store.home / "runs" / "run1"
    run.mkdir(parents=True)
    entries = []
    for name, extra in files:
        path = run / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x", encoding="utf-8")
        entries.append({"path": str(path), "sha256": "0" * 64, **extra})
    body = {"files": entries}
    if primary_subtask:
        body["primary_subtask"] = primary_subtask
    (run / "manifest.json").write_text(json.dumps(body), encoding="utf-8")
    return run


def test_files_from_the_last_subtask_are_primary(tmp_path):
    store = _store(tmp_path)
    _manifest(store, [
        ("collect/notes.md", {"subtask": "collect", "primary": False}),
        ("write/final.md", {"subtask": "write", "primary": True}),
    ], primary_subtask="write")
    items = list_artifacts(store, "run1")
    by_name = {i["name"]: i for i in items}
    assert by_name["final.md"]["primary"] is True
    assert by_name["notes.md"]["primary"] is False


def test_old_manifest_falls_back_and_says_it_is_inferred(tmp_path):
    store = _store(tmp_path)
    _manifest(store, [("a.md", {}), ("b.md", {})])
    items = list_artifacts(store, "run1")
    assert [i["primary"] for i in items] == [False, True]
    assert all(i["attribution"] == "inferred" for i in items)


def test_board_request_is_reported_when_present(tmp_path):
    from fusion.core import Store  # noqa: F401
    import urllib.request
    tmp = tmp_path
    priv, pub = tmp / "p.pem", tmp / "u.pem"
    subprocess.run(["openssl", "genpkey", "-algorithm", "ed25519", "-out", str(priv)], capture_output=True, check=True)
    subprocess.run(["openssl", "pkey", "-in", str(priv), "-pubout", "-out", str(pub)], capture_output=True, check=True)
    httpd = serve(tmp / "db", port=0, board_key=pub)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        _manifest(httpd.store, [("final.md", {"subtask": "write", "primary": True})],
                  primary_subtask="write")
        (httpd.store.home / "runs" / "run1" / "board-request.json").write_text("{}", encoding="utf-8")
        token = "a" * 8 + "b7f3c9d1e2a4" * 3 + "f0e1d2c3b4a5"
        httpd.api.add_token("t", token)
        req = urllib.request.Request(f"http://127.0.0.1:{httpd.server_port}/api/artifacts?run=run1")
        req.add_header("Authorization", f"Bearer {token}")
        with urllib.request.urlopen(req, timeout=5) as r:
            payload = json.loads(r.read())
        assert payload["board_request"]["exists"] is True
        assert payload["artifacts"][0]["primary"] is True
    finally:
        httpd.shutdown()
        httpd.server_close()
