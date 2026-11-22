from fusion.alerts import acknowledge, list_alerts, raise_alert


def test_alert_dedup_and_ack(tmp_path):
    first = raise_alert(tmp_path, "lifecycle_exhausted", "codex", "failed", "limit", ref="ISS-1")
    second = raise_alert(tmp_path, "lifecycle_exhausted", "codex", "failed", "limit again", ref="ISS-1")
    assert first == second
    rows = list_alerts(tmp_path)
    assert len(rows) == 1 and rows[0]["agent"] == "codex"
    assert acknowledge(tmp_path, first) is True
    assert list_alerts(tmp_path) == []
    assert list_alerts(tmp_path, True)[0]["acknowledged"] == 1
