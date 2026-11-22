from fusion.agent_lifecycle import health, restart, save_config, start, status, stop, supervise


def test_opt_in_lifecycle_starts_and_stops(tmp_path):
    save_config(tmp_path, "demo", {"start": ["/bin/sleep", "60"]})
    started = start(tmp_path, "demo")
    try:
        assert started["running"] and started["pid"]
        assert status(tmp_path, "demo")["running"]
        again = start(tmp_path, "demo")
        assert again["started"] is False
        restarted = restart(tmp_path, "demo")
        assert restarted["running"]
    finally:
        stopped = stop(tmp_path, "demo")
    assert stopped["stopped"] is True
    assert status(tmp_path, "demo")["running"] is False


def test_supervisor_restarts_always_and_respects_pause(tmp_path):
    save_config(tmp_path, "demo", {"start": ["/bin/sleep", "60"],
                                   "restart": "always", "max_restarts": 1,
                                   "restart_backoff_seconds": 5})
    try:
        assert supervise(tmp_path, now=1000)
        assert status(tmp_path, "demo")["running"]
        stop(tmp_path, "demo")
        assert supervise(tmp_path, now=2000) == []
    finally:
        stop(tmp_path, "demo")


def test_health_probe_and_on_failure_policy(tmp_path):
    save_config(tmp_path, "demo", {"start": ["/bin/sleep", "60"],
                                   "health": ["/bin/false"],
                                   "restart": "on-failure",
                                   "restart_backoff_seconds": 5})
    try:
        start(tmp_path, "demo")
        assert health(tmp_path, "demo") is False
    finally:
        stop(tmp_path, "demo")
