from fusion.core import Store
from fusion.issues import IssueStore
from fusion.tests.test_auth import call, http  # noqa: F401 - fixture
from fusion.users import UserStore
from fusion.workspaces import add_member, consume, create_invite, ensure, leave, members, rename, set_role, transfer_owner


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


def test_workspace_members_and_owner(tmp_path):
    users = UserStore(tmp_path / "auth")
    users.add_user("alice", "alice-password-1", "admin", workspace="w_team")
    users.add_user("bob", "bob-password-1", "operator", workspace="w_team")
    ensure(tmp_path, "w_team", "alice")
    add_member(tmp_path, "w_team", "bob", "member")
    data = members(tmp_path, "w_team")
    assert data["workspace"]["owner"] == "alice"
    assert {item["name"] for item in data["members"]} == {"alice", "bob"}
    assert next(item for item in data["members"] if item["name"] == "alice")["owner"] is True


def test_workspace_role_transfer_and_leave(tmp_path):
    users = UserStore(tmp_path / "auth")
    users.add_user("alice", "alice-password-1", "admin", workspace="w_team")
    users.add_user("bob", "bob-password-1", "operator", workspace="w_team")
    ensure(tmp_path, "w_team", "alice")
    add_member(tmp_path, "w_team", "bob", "member")
    set_role(tmp_path, "w_team", "alice", "bob", "admin")
    assert next(m for m in members(tmp_path, "w_team")["members"] if m["name"] == "bob")["workspace_role"] == "admin"
    meta = transfer_owner(tmp_path, "w_team", "alice", "bob")
    assert meta["owner"] == "bob"
    leave(tmp_path, "w_team", "alice")
    assert {m["name"] for m in members(tmp_path, "w_team")["members"]} == {"bob"}


def test_workspace_rename(tmp_path):
    users = UserStore(tmp_path / "auth")
    users.add_user("alice", "alice-password-1", "admin", workspace="w_team")
    ensure(tmp_path, "w_team", "alice")
    assert rename(tmp_path, "w_team", "alice", "Team Alpha")["name"] == "Team Alpha"
    try:
        rename(tmp_path, "w_team", "alice", "x")
        assert False
    except ValueError:
        pass


def test_workspace_owner_can_write_even_with_viewer_global_role(http):
    httpd, base = http
    httpd.users.add_user("viewerowner", "viewerowner-password-1", "viewer", workspace="w_team")
    ensure(httpd.store.home, "w_team", "viewerowner")
    _, login = call(base, "/api/login", method="POST", body={
        "username": "viewerowner", "password": "viewerowner-password-1"})
    status, issue = call(base, "/api/issues", login["session"], "POST", {
        "title": "owner write", "assignee": "gongbu"})
    assert status == 201 and issue["state"] == "queued"
