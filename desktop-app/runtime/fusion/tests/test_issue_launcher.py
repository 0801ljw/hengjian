from fusion.core import Store
from fusion.issues import IssueStore
from fusion.launchers import claim, create_pairing_code
from fusion.tests.test_auth import call, http  # noqa: F401 - fixture


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
    done = issues.launcher_complete(issue["id"], paired["launcher_id"], token, "ok",
                                    [{"path": "x.txt"}], [{"type": "file_exists", "path": "x.txt"}])
    assert done["state"] == "done"
    assert done["result"]["executed_by"].startswith("launcher:")


def test_launcher_dispatch_api(http):
    httpd, base = http
    httpd.users.add_user("root", "root-password-1", "admin")
    _, login = call(base, "/api/login", method="POST", body={
        "username": "root", "password": "root-password-1"})
    session = login["session"]
    _, pair = call(base, "/api/launchers/pair", session, "POST", {})
    _, claimed = call(base, "/api/launchers/claim", method="POST", body={
        "code": pair["code"], "name": "api-launcher", "host": "test"})
    launcher_id, token = claimed["launcher_id"], claimed["token"]
    _, issue = call(base, "/api/issues", session, "POST", {
        "title": "api dispatch", "assignee": "gongbu", "launcher_id": launcher_id})
    _, tasks = call(base, "/api/launcher/tasks", token)
    assert [item["id"] for item in tasks["tasks"]] == [issue["id"]]
    _, claimed_task = call(base, f"/api/launcher/tasks/{issue['id']}/claim", token, "POST", {})
    _, done = call(base, f"/api/launcher/tasks/{issue['id']}/complete", token, "POST", {
        "claim_token": claimed_task["claim_token"], "summary": "remote",
        "artifacts": [], "checks": []})
    assert done["state"] == "done"
