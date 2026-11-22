import stat

import json

from fusion.agent_workspace import new_run, runs, workspace


def test_agent_workspace_and_run_are_private(tmp_path):
    space = workspace(tmp_path, "codex")
    assert space.name == "workspace"
    assert stat.S_IMODE(space.stat().st_mode) == 0o700
    run = new_run(tmp_path, "codex", "ISS-test")
    assert run.parent.name == "runs"
    assert run.exists()
    assert stat.S_IMODE(run.parent.stat().st_mode) == 0o700


def test_run_history_is_bounded_and_redacted(tmp_path):
    run = new_run(tmp_path, "codex", "ISS-demo")
    (run / "round").mkdir()
    (run / "round" / "decisions.jsonl").write_text(json.dumps({
        "step": "issue_execute", "status": "valid", "model": "m",
        "returned_model": "m", "at": "2026-01-01T00:00:00+00:00"}) + "\n")
    (run / "artifacts").mkdir()
    (run / "artifacts" / "a.txt").write_text("OK")
    rows = runs(tmp_path, "codex")
    assert rows[0]["issue_id"] == "ISS-demo"
    assert rows[0]["steps"][0]["step"] == "issue_execute"
    assert rows[0]["artifacts"] == ["a.txt"]
