from fusion.launchers import claim, create_pairing_code, heartbeat, list_launchers, revoke


def test_pairing_and_heartbeat(tmp_path):
    code = create_pairing_code(tmp_path, "owner")
    assert len(code["code"]) == 8
    claimed = claim(tmp_path, code["code"], "Mac", "macbook", "1.0")
    assert claimed["launcher_id"] and claimed["token"]
    rows = list_launchers(tmp_path)
    assert rows[0]["name"] == "Mac" and rows[0]["owner"] == "owner"
    assert heartbeat(tmp_path, claimed["launcher_id"], claimed["token"]) is True
    assert heartbeat(tmp_path, claimed["launcher_id"], "wrong") is False
    assert revoke(tmp_path, claimed["launcher_id"], "other") is False
    assert revoke(tmp_path, claimed["launcher_id"], "owner") is True
    assert list_launchers(tmp_path) == []
