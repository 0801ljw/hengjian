"""Bounded HTTP smoke test over the saved real execution; no model invocation."""
import argparse
import hashlib
import json
import threading
import urllib.request
from pathlib import Path

from .core import Store
from .dashboard import make_server


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--home", type=Path, default=Path("fusion/var"))
    parser.add_argument("--company", default="default")
    args = parser.parse_args()
    store = Store(args.home, company=args.company)
    server = make_server(store, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    checks = {}
    try:
        for path in ("/", "/api/live-status", "/api/fusion/status"):
            with urllib.request.urlopen(base + path, timeout=3) as response:
                assert response.status == 200
                checks[path] = response.status
                if path == "/api/fusion/status":
                    snapshot = json.load(response)
        run = next(r for r in snapshot["runs"] if r["status"] == "succeeded")
        task = next(t for t in snapshot["tasks"] if t["id"] == run["task_id"])
        assert task["state"] == "Done"
        assert [f["to"] for f in task["flow_log"]] == [
            "Zhongshu", "Menxia", "Assigned", "Doing", "Review", "PendingConfirm", "Done"]
        path = Path(run["artifact"]["path"])
        assert hashlib.sha256(path.read_bytes()).hexdigest() == run["artifact"]["sha256"]
        assert json.loads(path.read_text()) == {"totals": {"gongbu": 22, "hubu": 32}, "row_count": 4}
        print(json.dumps({"http": checks, "task_state": task["state"], "run_id": run["id"],
                          "artifact": run["artifact"], "usage": run["usage"],
                          "cost_usd": run["cost_usd"]}, ensure_ascii=False, indent=2))
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


if __name__ == "__main__":
    main()
