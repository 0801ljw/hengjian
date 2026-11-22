from fusion.core import Store
from fusion.issues import IssueStore
from fusion.tests.test_auth import call, http  # noqa: F401 - fixture
from fusion.users import UserStore
from fusion.workspaces import consume, create_invite


def test_new_users_get_distinct_workspaces(tmp_path):
    users = UserStore(tmp_path / "auth")
    users.add_user("alice", "alice-password-1", "operator")
    users.add_user("bob", "bob-password-1", "operator")
    assert users.workspace("alice") != users.workspace("bob")
    assert users.workspace("alice").startswith("w_")


def test_issue_api_is_workspace_scoped(http):
    httpd, base = http
    httpd.users.add_user("alice", "alice-password-1", "operator")
    httpd.users.add_user("bob", "bob-password-1", "operator")
    _, a = call(base, "/api/login", method="POST", body={"username": "alice", "password": "alice-password-1"})
    _, b = call(base, "/api/login", method="POST", body={"username": "bob", "password": "bob-password-1"})
    assert a["workspace"] != b["workspace"]
    _, issue = call(base, "/api/issues", a["session"], "POST", {"title": "alice", "assignee": "gongbu"})
    _, alice = call(base, "/api/issues", a["session"])
    _, bob = call(base, "/api/issues", b["session"])
    assert [item["id"] for item in alice["issues"]] == [issue["id"]]
    assert bob["issues"] == []
    assert call(base, f"/api/issues/{issue['id']}", b["session"])[0] == 404


def test_workspace_invite_is_one_use(tmp_path):
    invite = create_invite(tmp_path, "w_team", "board")
    assert consume(tmp_path, invite["code"]) == "w_team"
    try:
        consume(tmp_path, invite["code"])
        assert False
    except ValueError:
        pass
