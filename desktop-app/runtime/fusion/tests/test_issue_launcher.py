from fusion.core import Store
from fusion.issues import IssueStore
from fusion.launchers import claim, create_pairing_code


def test_issue_can_be_dispatched_to_launcher(tmp_path):
    store = Store(tmp_path / "db")
    issues = IssueStore(store)
    code = create_pairing_code(store.home, "owner")
    paired = claim(store.home, code["code"], "Mac", "macbook", "1")
    issue = issues.create(title="remote", assignee="gongbu", body="do it",
                          launcher_id=paired["launcher_id"])
    tasks = issues.launcher_tasks(paired["launcher_id"])
    assert [item["id"] for item in tasks] == [issue["id"]]
    claimed, token = issues.launcher_claim(issue["id"], paired["launcher_id"])
    assert claimed["state"] == "running"
    assert issues.launcher_heartbeat(issue["id"], paired["launcher_id"], token) is True
    done = issues.launcher_complete(issue["id"], paired["launcher_id"], token, "ok",
                                    [{"path": "x.txt"}], [{"type": "file_exists", "path": "x.txt"}],
                                    files=[{"path": "x.txt", "content": "hello"}])
    assert done["state"] == "done"
    assert done["result"]["executed_by"].startswith("launcher:")
    assert issues.files(issue["id"])[0]["path"] == "x.txt"
    assert issues.file(issue["id"], "x.txt")["content"] == "hello"
    actions = [item["action"] for item in issues.timeline(issue["id"])]
    assert "created" in actions and "launcher_claimed" in actions and "launcher_completed" in actions


def test_blocked_issue_retry_and_launcher_release(tmp_path):
    store = Store(tmp_path / "db")
    issues = IssueStore(store)
    code = create_pairing_code(store.home, "owner")
    paired = claim(store.home, code["code"], "Mac", "macbook", "1")
    issue = issues.create(title="retry", assignee="gongbu", max_attempts=1,
                          launcher_id=paired["launcher_id"])
    claimed, token = issues.launcher_claim(issue["id"], paired["launcher_id"])
    assert [item["id"] for item in issues.launcher_tasks(paired["launcher_id"], "running")] == [issue["id"]]
    issues.launcher_release(issue["id"], paired["launcher_id"], token)
    assert issues.get(issue["id"])["state"] == "queued"
    claimed, token = issues.launcher_claim(issue["id"], paired["launcher_id"])
    issues.fail(issue["id"], token, "boom", retry=False)
    assert issues.get(issue["id"])["state"] == "blocked"
    retried = issues.retry(issue["id"], "human")
    assert retried["state"] == "queued" and retried["attempts"] == 0


def test_launcher_binary_upload(tmp_path):
    store = Store(tmp_path / "db")
    issues = IssueStore(store)
    code = create_pairing_code(store.home, "owner")
    paired = claim(store.home, code["code"], "Mac", "macbook", "1")
    issue = issues.create(title="binary", assignee="gongbu", launcher_id=paired["launcher_id"])
    _, token = issues.launcher_claim(issue["id"], paired["launcher_id"])
    blob = b"\x89PNG\r\n\x1a\n" + b"x" * 32
    issues.launcher_upload_file(issue["id"], paired["launcher_id"], token, "image.png", blob)
    files = issues.files(issue["id"])
    assert files[0]["path"] == "image.png" and files[0]["download"] is True
    item = issues.file(issue["id"], "image.png")
    assert item["binary"] is True and item["size"] == len(blob) and item["storage_path"]
    assert (tmp_path / "db" / item["storage_path"]).is_file()


def test_delete_file_removes_storage(tmp_path):
    store = Store(tmp_path / "db")
    issues = IssueStore(store)
    code = create_pairing_code(store.home, "owner")
    paired = claim(store.home, code["code"], "Mac", "macbook", "1")
    issue = issues.create(title="delete", assignee="gongbu", launcher_id=paired["launcher_id"])
    _, token = issues.launcher_claim(issue["id"], paired["launcher_id"])
    issues.launcher_upload_file(issue["id"], paired["launcher_id"], token, "image.png",
                                b"\x89PNG\r\n\x1a\n" + b"x" * 32)
    item = issues.file(issue["id"], "image.png")
    storage = tmp_path / "db" / item["storage_path"]
    assert storage.is_file()
    issues.delete_file(issue["id"], "image.png")
    assert issues.files(issue["id"]) == []
    assert not storage.exists()


def test_execution_events_are_structured(tmp_path):
    store = Store(tmp_path / "db")
    issues = IssueStore(store)
    code = create_pairing_code(store.home, "owner")
    paired = claim(store.home, code["code"], "Mac", "macbook", "1")
    issue = issues.create(title="events", assignee="gongbu", launcher_id=paired["launcher_id"])
    _, token = issues.launcher_claim(issue["id"], paired["launcher_id"])
    import json
    lines = "\n".join([
        json.dumps({"step": "issue_execute", "status": "valid", "model": "m", "at": "t"}),
        json.dumps({"step": "issue_execute", "status": "error", "error": "boom"}),
    ]) + "\n"
    issues.launcher_upload_file(issue["id"], paired["launcher_id"], token, "logs/decisions.jsonl",
                                lines.encode())
    events = issues.execution_events(issue["id"])
    assert [e["status"] for e in events] == ["valid", "error"]


def test_prune_logs_keeps_artifacts(tmp_path):
    store = Store(tmp_path / "db")
    issues = IssueStore(store)
    code = create_pairing_code(store.home, "owner")
    paired = claim(store.home, code["code"], "Mac", "macbook", "1")
    issue = issues.create(title="retention", assignee="gongbu", launcher_id=paired["launcher_id"])
    _, token = issues.launcher_claim(issue["id"], paired["launcher_id"])
    issues.launcher_upload_file(issue["id"], paired["launcher_id"], token, "keep.txt", b"keep")
    issues.launcher_upload_file(issue["id"], paired["launcher_id"], token, "logs/decisions.jsonl", b"{}\n")
    with issues.store.connect() as conn:
        conn.execute("UPDATE issue_files SET created_at='2000-01-01T00:00:00+00:00' WHERE path='logs/decisions.jsonl'")
    result = issues.prune_logs(30)
    assert result["deleted"] == 1
    assert [f["path"] for f in issues.files(issue["id"])] == ["keep.txt"]
