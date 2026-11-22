"""P8 runtime facts and durable, redacted worker failure diagnostics; offline."""
import json
import logging
import os
import time
from datetime import datetime
from types import SimpleNamespace

import pytest

from fusion import ai, ai_view, server
from fusion.ai_pipeline import Pipeline
from fusion.core import PolicyError, Store
from fusion.httpapi import API
from fusion.tests.test_ai import DISPATCH, GOOD, PLAN, ROUTE, WORK, FakeTransport


def test_runtime_context_present(tmp_path):
    store = Store(tmp_path / "instance")
    transport = FakeTransport([ROUTE, PLAN, GOOD, DISPATCH, WORK])
    client = ai.DecisionClient(store.home / "ai" / "AI-runtime", transport)
    result = Pipeline(store, client, "AI-runtime").run("write today's date", allow_unpriced=True)
    contexts = [json.loads(r["messages"][1]["content"])["input"] for r in transport.requests]
    assert "runtime" not in contexts[0]
    for context in contexts[1:4]:
        assert set(context["runtime"]) == {"now", "workspace"}
        assert context["runtime"]["workspace"] == str(store.home)
        assert datetime.fromisoformat(context["runtime"]["now"]).utcoffset() is not None
    runtime = contexts[-1]["runtime"]
    assert runtime["workspace"] == result["workspace"]
    assert runtime["role"] == "gongbu"
    assert runtime["limits"] == {"max_file_bytes": 262144, "no_network": True, "no_commands": True}
    assert runtime["timezone"]
    assert datetime.fromisoformat(runtime["now"]).utcoffset() is not None
    assert "你拿不到时钟、网络和命令，时间只能取 runtime.now" in transport.requests[-1]["messages"][0]["content"]


@pytest.mark.parametrize("zone", ["UTC", "Asia/Shanghai", "America/New_York"])
def test_runtime_timezone_is_local(tmp_path, zone):
    original = os.environ.get("TZ")
    try:
        os.environ["TZ"] = zone
        time.tzset()
        runtime = ai.runtime_context(tmp_path, "gongbu")
        parsed = datetime.fromisoformat(runtime["now"])
        assert parsed.utcoffset() == datetime.now().astimezone().utcoffset()
        assert abs((parsed - datetime.now().astimezone()).total_seconds()) < 5
    finally:
        if original is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = original
        time.tzset()


def queued_api(tmp_path, count=1):
    api = API(Store(tmp_path))
    token = "fixture-worker-token-" + "a" * 32
    api.add_token("fixture", token)
    headers = {"Authorization": "Bearer " + token}
    ids = [api.handle("POST", "/api/requests", headers, {"request": "write"})[1]["id"]
           for _ in range(count)]
    return api, headers, ids


@pytest.mark.parametrize("during_execution", [False, True])
def test_worker_records_swallowed_error(tmp_path, monkeypatch, during_execution):
    api, headers, ids = queued_api(tmp_path)
    private = "secret /private/path Traceback\nprovider credential"

    def factory(audit, **kwargs):
        class Broken:
            def decide(self, *args):
                raise PolicyError(private)
        return Broken()

    if during_execution:
        original = ai.DecisionClient
        monkeypatch.setattr(ai, "DecisionClient", lambda audit: original(
            audit, FakeTransport([ROUTE, PLAN, GOOD, DISPATCH, PolicyError(private)])))
    else:
        monkeypatch.setattr(ai, "DecisionClient", factory)
    httpd = SimpleNamespace(api=api, store=api.store, stop_worker=False)
    process = api.process_next

    def once(*args, **kwargs):
        try:
            return process(*args, **kwargs)
        finally:
            httpd.stop_worker = True

    monkeypatch.setattr(api, "process_next", once)
    server.worker_loop(httpd, interval=0, allow_unpriced=True)
    restarted = API(api.store)
    status, detail = restarted.handle("GET", "/api/runs/" + ids[0], headers, None)
    assert status == 200 and detail["status"] == "error"
    error = detail["error"]
    assert error["error_type"] == "PolicyError"
    assert set(error) == {"error_type", "message", "at"}
    assert datetime.fromisoformat(error["at"])
    assert "Traceback" not in json.dumps(detail)
    assert "/private/path" not in json.dumps(detail) and "credential" not in json.dumps(error)
    saved = json.loads((api.queue / (ids[0] + ".json")).read_text())
    assert saved["result"]["error"] == error
    assert ai_view.read_runs(api.store)[0]["error"] == error
    if during_execution:
        assert detail["runs"][0]["status"] == "failed"


def test_worker_repeated_failure_logs_once_per_three(tmp_path, monkeypatch, caplog):
    api, _, ids = queued_api(tmp_path, 6)

    def broken(*args, **kwargs):
        raise PolicyError("private details")

    monkeypatch.setattr(ai, "DecisionClient", broken)
    httpd = SimpleNamespace(api=api, store=api.store, stop_worker=False)
    process = api.process_next
    count = 0

    def consume(*args, **kwargs):
        nonlocal count
        result = process(*args, **kwargs)
        count += 1
        httpd.stop_worker = count == len(ids)
        return result

    monkeypatch.setattr(api, "process_next", consume)
    with caplog.at_level(logging.ERROR, logger="fusion.server"):
        server.worker_loop(httpd, interval=0)
    assert caplog.messages == ["worker failing repeatedly: PolicyError"] * 2


def test_worker_failure_counter_resets_on_success_and_type_change(tmp_path, caplog):
    httpd = SimpleNamespace(stop_worker=False)
    results = iter([PolicyError("private")] * 2 + [ValueError("private")] * 2
                   + [{"status": "chat"}] + [ValueError("private")] * 3)

    def consume(*args, **kwargs):
        try:
            result = next(results)
        except StopIteration:
            httpd.stop_worker = True
            return None
        if isinstance(result, Exception):
            raise result
        return result

    httpd.api = SimpleNamespace(process_next=consume)
    with caplog.at_level(logging.ERROR, logger="fusion.server"):
        server.worker_loop(httpd, interval=0)
    repeated = [message for message in caplog.messages if "failing repeatedly" in message]
    assert repeated == ["worker failing repeatedly: ValueError"]
    assert "private" not in caplog.text


@pytest.mark.parametrize("worker,allow,expected", [(True, False, True), (True, True, False),
                                                  (False, False, False)])
def test_worker_warns_when_unpriced_disabled(tmp_path, monkeypatch, caplog, worker, allow, expected):
    monkeypatch.setattr(server, "worker_loop", lambda *args, **kwargs: None)
    with caplog.at_level(logging.WARNING, logger="fusion.server"):
        httpd = server.serve(tmp_path, worker=worker, allow_unpriced=allow)
    try:
        warning = "worker running with unpriced usage disabled: queued runs will fail"
        assert (warning in caplog.messages) == expected
    finally:
        if httpd.runner:
            httpd.runner.join()
        httpd.server_close()


def test_worker_unpriced_failure_has_actionable_safe_summary(tmp_path):
    api, headers, ids = queued_api(tmp_path)
    transport = FakeTransport([ROUTE, PLAN, GOOD, DISPATCH, WORK])
    api.process_next(lambda audit: ai.DecisionClient(audit, transport))
    detail = api.handle("GET", "/api/runs/" + ids[0], headers, None)[1]
    assert detail["error"]["error_type"] == "BudgetError"
    assert "Unpriced usage is disabled" in detail["error"]["message"]
    assert len(transport.requests) == 4
    assert api.store.snapshot()["runs"] == []


def test_worker_does_not_warn_when_board_policy_allows(tmp_path, monkeypatch, caplog):
    from fusion import budget
    monkeypatch.setattr(budget, "policy", lambda conn: {"unpriced": "allow"})
    monkeypatch.setattr(server, "worker_loop", lambda *args, **kwargs: None)
    with caplog.at_level(logging.WARNING, logger="fusion.server"):
        httpd = server.serve(tmp_path, worker=True)
    try:
        assert not caplog.messages
    finally:
        httpd.runner.join()
        httpd.server_close()
