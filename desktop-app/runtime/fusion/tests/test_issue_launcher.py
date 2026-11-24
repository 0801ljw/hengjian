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
                                    [{"path": "x.txt"}], [{"type": "file_exists", "path": "x.txt"}])
    assert done["state"] == "done"
    assert done["result"]["executed_by"].startswith("launcher:")
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
    issues.launcher_release(issue["id"], paired["launcher_id"], token)
    assert issues.get(issue["id"])["state"] == "queued"
    claimed, token = issues.launcher_claim(issue["id"], paired["launcher_id"])
    issues.fail(issue["id"], token, "boom", retry=False)
    assert issues.get(issue["id"])["state"] == "blocked"
    retried = issues.retry(issue["id"], "human")
    assert retried["state"] == "queued" and retried["attempts"] == 0
