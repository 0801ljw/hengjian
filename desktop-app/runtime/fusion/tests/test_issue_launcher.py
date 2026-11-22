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
