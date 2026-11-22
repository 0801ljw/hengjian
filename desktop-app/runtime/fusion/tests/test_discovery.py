import json
import stat
import threading
import urllib.request

from fusion.server import serve


def test_discovery_file_exposes_local_token(tmp_path):
    discovery = tmp_path / "instance.json"
    httpd = serve(tmp_path / "db", port=0, discovery_file=discovery)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        data = json.loads(discovery.read_text())
        assert data["port"] == httpd.server_port
        assert data["pid"] > 0 and data["token"]
        assert stat.S_IMODE(discovery.stat().st_mode) == 0o600
        req = urllib.request.Request(
            f"http://127.0.0.1:{data['port']}/api/health",
            headers={"Authorization": "Bearer " + data["token"]},
        )
        with urllib.request.urlopen(req, timeout=5) as response:
            assert response.status == 200
    finally:
        httpd.shutdown()
        httpd.server_close()
