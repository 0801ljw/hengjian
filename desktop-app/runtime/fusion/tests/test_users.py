from fusion.users import UserStore


def test_remove_user_invalidates_sessions(tmp_path):
    store = UserStore(tmp_path)
    store.add_user("alice", "password123", "operator")
    session = store.login("alice", "password123")
    assert store.check_session(session["token"])["name"] == "alice"
    store.remove_user("alice")
    assert store.check_session(session["token"]) is None


def test_session_role_follows_current_user_record(tmp_path):
    import json
    store = UserStore(tmp_path)
    store.add_user("alice", "password123", "operator")
    session = store.login("alice", "password123")
    data = json.loads(store.path.read_text())
    data["users"][0]["role"] = "admin"
    store.path.write_text(json.dumps(data))
    assert store.check_session(session["token"])["role"] == "admin"
