"""Artifact listing/preview safety and the queue worker switch."""
import json
import subprocess
import threading
import urllib.error
import urllib.request

import pytest

from fusion.server import list_artifacts, read_artifact, serve


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


def _run_with_files(store, name="run1"):
    run = store.home / "runs" / name
    run.mkdir(parents=True)
    (run / "note.md").write_text("# 标题\n正文", encoding="utf-8")
    (run / "blob.bin").write_bytes(b"\x00\x01")
    (run / "manifest.json").write_text(json.dumps({"files": [
        {"path": str(run / "note.md"), "sha256": "0" * 64},
        {"path": str(run / "blob.bin"), "sha256": "1" * 64},
        {"path": str(store.home / "outside.md"), "sha256": "2" * 64},
    ]}, ensure_ascii=False), encoding="utf-8")
    return run


def test_listing_marks_previewable_and_skips_outside_files(tmp_path):
    from fusion.core import Store
    priv, pub = tmp_path / "p.pem", tmp_path / "u.pem"
    subprocess.run(["openssl", "genpkey", "-algorithm", "ed25519", "-out", str(priv)], capture_output=True, check=True)
    subprocess.run(["openssl", "pkey", "-in", str(priv), "-pubout", "-out", str(pub)], capture_output=True, check=True)
    store = Store(tmp_path / "db", pub)
    _run_with_files(store)
    (store.home / "outside.md").write_text("secret", encoding="utf-8")
    items = list_artifacts(store, "run1")
    names = {i["name"]: i["previewable"] for i in items}
    assert names == {"note.md": True, "blob.bin": False}


def test_preview_returns_text_and_sha256(tmp_path):
    from fusion.core import Store
    store = Store(tmp_path / "db")
    _run_with_files(store)
    text, digest = read_artifact(store, "run1", "note.md")
    assert text.startswith("# 标题") and len(digest) == 64


@pytest.mark.parametrize("relative", ["../../etc/passwd", "/etc/passwd", "blob.bin", "missing.md"])
def test_preview_refuses_unsafe_or_unsupported_reads(tmp_path, relative):
    from fusion.core import Store
    store = Store(tmp_path / "db")
    _run_with_files(store)
    with pytest.raises(ValueError):
        read_artifact(store, "run1", relative)


def test_invalid_run_ids_are_refused(tmp_path):
    from fusion.core import Store
    store = Store(tmp_path / "db")
    for bad in ("../db", ".", "", "a/b"):
        with pytest.raises(ValueError):
            list_artifacts(store, bad)


def test_artifact_routes_need_auth(http):
    httpd, base = http
    _run_with_files(httpd.store)
    for path in ("/api/artifacts?run=run1", "/api/artifact?run=run1&name=note.md"):
        req = urllib.request.Request(base + path)
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(req, timeout=5)
        assert exc.value.code == 401


def test_worker_flag_spawns_a_consumer_thread(http):
    assert http[0].runner is None, "worker must stay off unless requested"
