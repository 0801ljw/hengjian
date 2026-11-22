"""Scheduled stuck audit wrapper: read-only, exits 2 only when something is stuck."""
import json
import os
import subprocess
from pathlib import Path

from fusion.core import Store
from fusion.tests.board_fixture import keypair

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "fusion" / "stuck-check.sh"


def _run(home: Path, status: Path):
    env = dict(os.environ)
    env.update({
        "FUSION_HOME": str(home),
        "FUSION_STUCK_STATUS": str(status),
        "FUSION_STUCK_HOURS": "0",
        "PYTHONPATH": str(REPO),
    })
    return subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True,
                          env=env, cwd=str(REPO), timeout=60)


def test_scheduled_audit_is_quiet_when_nothing_is_pending(tmp_path):
    _, pub = keypair(tmp_path / "keys")
    Store(tmp_path / "instance", pub)
    status = tmp_path / "status.json"
    result = _run(tmp_path / "instance", status)
    assert result.returncode == 0, result.stderr
    document = json.loads(status.read_text(encoding="utf-8"))
    assert document["ok"] is True and document["stuck"] == 0
    assert document["non_terminal"] == 0


def test_scheduled_audit_reports_stuck_without_touching_state(tmp_path):
    _, pub = keypair(tmp_path / "keys")
    store = Store(tmp_path / "instance", pub)
    store.create("T", "scheduled fixture")
    store.move("T", "taizi", "Zhongshu", "fixture")
    store.move("T", "zhongshu", "Menxia", "fixture")
    status = tmp_path / "status.json"
    result = _run(tmp_path / "instance", status)
    assert result.returncode == 2, result.stderr
    assert "WARN:" in result.stderr
    document = json.loads(status.read_text(encoding="utf-8"))
    assert document["ok"] is False and document["stuck"] == 1
    assert document["reports"][0]["kind"] == "stalled_menxia"
    assert document["applied"] == []
    # The scheduled audit is read-only: the task must not have moved.
    assert store.get("T")["state"] == "Menxia"
