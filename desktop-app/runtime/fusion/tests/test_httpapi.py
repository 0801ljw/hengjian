"""Offline HTTP boundary acceptance; no server or paid provider is started."""
import json
import secrets
from concurrent.futures import ThreadPoolExecutor

import pytest

from fusion import httpapi
from fusion.ai import DecisionClient
from fusion.core import Store
from fusion.tests.test_ai import DISPATCH, GOOD, PLAN, ROUTE, WORK, FakeTransport


@pytest.fixture
def api(tmp_path):
    return httpapi.API(Store(tmp_path))


@pytest.fixture
def headers(api):
    token = secrets.token_urlsafe(32)
    api.add_token("alice", token)
    return {"Authorization": "Bearer " + token}


def records(api):
    return [json.loads(line) for line in api.audit.read_text().splitlines()]


@pytest.mark.parametrize("path", ["/api/health", "/api/tasks", "/api/tasks/x",
                                     "/api/requests", "/api/runs/x", "/api/budget", "/unknown"])
@pytest.mark.parametrize("auth", [{}, {"Authorization": "Bearer wrong"},
                                  {"Authorization": "Basic invalid"}])
def test_auth_failure_uniform_before_dispatch(api, path, auth):
    assert api.handle("POST", path, auth, {"request": "write"}) == (
        401, {"error": "unauthorized"})
    assert not api.queue.exists()
    assert records(api)[-1]["who"] == "anonymous"
    assert records(api)[-1]["status"] == 401


@pytest.mark.parametrize("content", [None, "not json", "[]", '{"tokens": null}',
                                      '{"tokens": [{"name": "leak", "token": "plaintext"}]}'])
def test_bad_token_configuration_fails_closed(api, content):
    if content is not None:
        api.tokens.write_text(content)
    assert api.handle("GET", "/api/health", {"Authorization": "Bearer " + "x" * 32}, None) == (
        401, {"error": "unauthorized"})


def test_provision_multiple_tokens_and_revoke(api, headers):
    token = secrets.token_urlsafe(32)
    api.add_token("bob", token)
    saved = api.tokens.read_text()
    assert token not in saved and headers["Authorization"].split()[1] not in saved
    assert api.tokens.stat().st_mode & 0o777 == 0o600
    assert api.handle("GET", "/api/health", {"authorization": "bearer " + token}, None) == (200, {"ok": True})
    assert records(api)[-1]["who"] == "bob"
    assert api.handle("GET", "/api/health", headers, None)[0] == 200
    data = json.loads(saved)
    data["tokens"] = [r for r in data["tokens"] if r["name"] != "bob"]
    api.tokens.write_text(json.dumps(data))
    assert api.handle("GET", "/api/health", {"Authorization": "Bearer " + token}, None)[0] == 401
    with pytest.raises(ValueError):
        api.add_token("alice", secrets.token_urlsafe(32))
    with pytest.raises(ValueError):
        api.add_token("weak", "short")


def test_duplicate_authorization_headers_rejected(api, headers):
    headers["authorization"] = headers["Authorization"]
    assert api.handle("GET", "/api/health", headers, None)[0] == 401


def test_read_endpoints_do_not_mutate_ledger(api, headers, monkeypatch):
    api.store.create("task-one", "a task")
    before = api.store.snapshot()
    monkeypatch.setattr(api.store, "transaction", lambda: pytest.fail("read mutated ledger"))
    assert api.handle("GET", "/api/tasks", headers, None) == (200, {"tasks": before["tasks"]})
    assert api.handle("GET", "/api/tasks/task-one", headers, None) == (200, before["tasks"][0])
    assert api.handle("GET", "/api/budget", headers, None)[0] == 200
    assert api.store.snapshot() == before


@pytest.mark.parametrize("method,path,status", [
    ("GET", "/api/requests", 405), ("POST", "/api/tasks", 405),
    ("DELETE", "/api/budget", 405), ("GET", "/unknown", 404),
    ("GET", "/api/tasks/missing", 404), ("GET", "/api/runs/missing", 404),
    ("GET", "/api/runs/../tokens.json", 404), ("GET", "/api/runs/%2e%2e", 404)])
def test_routing_boundaries(api, headers, method, path, status):
    assert api.handle(method, path, headers, None)[0] == status
    assert records(api)[-1]["status"] == status


@pytest.mark.parametrize("body", [None, [], {}, {"request": ""}, {"request": " "},
                                  {"request": 1}, {"request": "x" * 12001},
                                  {"request": "write", "allow_unpriced": True},
                                  {"request": "write", "company": "other"},
                                  b"\xff", "broken", "[]"])
def test_invalid_requests_not_queued(api, headers, body):
    assert api.handle("POST", "/api/requests", headers, body)[0] == 400
    assert not api.queue.exists()


def test_oversized_body(api, headers):
    assert api.handle("POST", "/api/requests", headers, "x" * 65537)[0] == 413


def submit(api, headers):
    status, response = api.handle("POST", "/api/requests", headers,
                                  json.dumps({"request": "写一个报告"}).encode())
    assert status == 200 and response["status"] == "queued"
    return response["id"]


def factory(values):
    return lambda path: DecisionClient(path, FakeTransport(values))


def test_durable_async_submission_and_steps(api, headers):
    before = api.store.snapshot()
    key = submit(api, headers)
    assert api.store.snapshot() == before  # POST does not run models or mutations
    restarted = httpapi.API(api.store)
    assert restarted.handle("GET", "/api/runs/" + key, headers, None)[1]["status"] == "queued"
    # Explicit offline consumer, actual Pipeline, not a mocked success response.
    result = restarted.process_next(factory([ROUTE, PLAN, GOOD, DISPATCH, WORK]), allow_unpriced=True)
    assert result == {"id": key, "status": "awaiting_board"}
    status, run = api.handle("GET", "/api/runs/" + key, headers, None)
    assert status == 200 and run["status"] == "awaiting_board"
    assert [step["actor"] for step in run["steps"]] == ["taizi", "zhongshu", "menxia", "shangshu", "gongbu"]
    assert run["artifacts"] and run["state"] == "Review"
    execution = run["runs"][0]["id"]
    assert api.handle("GET", "/api/runs/" + execution, headers, None)[1]["steps"] == run["steps"]
    assert not any(d["kind"] == "approval" for d in api.store.snapshot()["decisions"])
    assert api.process_next(factory([])) is None


def test_budget_gate_preserved(api, headers):
    key = submit(api, headers)
    transport = FakeTransport([ROUTE, PLAN, GOOD, DISPATCH, WORK])
    api.process_next(lambda path: DecisionClient(path, transport))
    assert len(transport.requests) == 4  # no worker call without budget admission
    assert not api.store.snapshot()["runs"]
    assert api.handle("GET", "/api/runs/" + key, headers, None)[1]["status"] == "error"


def test_lease_gate_preserved(api, headers, monkeypatch):
    from fusion import leases

    def busy(*args):
        raise leases.LeaseError("LEASE_BUSY")

    monkeypatch.setattr(leases, "acquire", busy)
    submit(api, headers)
    transport = FakeTransport([ROUTE, PLAN, GOOD, DISPATCH, WORK])
    assert api.process_next(lambda path: DecisionClient(path, transport), allow_unpriced=True)["status"] == "error"
    assert len(transport.requests) == 4 and not api.store.snapshot()["runs"]


def test_chat_and_failure_without_decision_audit(api, headers):
    key = submit(api, headers)
    assert api.process_next(factory([{**ROUTE, "kind": "chat"}]))["status"] == "chat"
    assert api.handle("GET", "/api/runs/" + key, headers, None)[1]["status"] == "chat"
    key = submit(api, headers)

    def broken(path):
        raise OSError("private internal detail")

    assert api.process_next(broken)["status"] == "error"
    status, run = api.handle("GET", "/api/runs/" + key, headers, None)
    assert status == 200 and run["status"] == "error" and run["steps"] == []
    assert "private internal detail" not in json.dumps(run)


def test_namespace_isolation(tmp_path):
    alpha = httpapi.API(Store(tmp_path, company="alpha"))
    beta = httpapi.API(Store(tmp_path, company="beta"))
    token = secrets.token_urlsafe(32)
    alpha.add_token("alpha", token)
    headers = {"Authorization": "Bearer " + token}
    key = submit(alpha, headers)
    assert beta.handle("GET", "/api/runs/" + key, headers, None)[0] == 401
    beta.add_token("beta", token)
    assert beta.handle("GET", "/api/runs/" + key, headers, None)[0] == 404


def test_audit_redacts_secrets_and_query(api, headers):
    assert api.handle("GET", "/api/health?token=private-query", headers, None)[0] == 200
    record = records(api)[-1]
    assert record["what"] == "GET /api/health" and record["time"]
    assert record["who"] == "alice" and record["status"] == 200
    assert "private-query" not in api.audit.read_text()
    assert headers["Authorization"].split()[1] not in api.audit.read_text()


def test_internal_error_generic_and_audited(api, headers, monkeypatch):
    monkeypatch.setattr(api.store, "snapshot", lambda: (_ for _ in ()).throw(RuntimeError("secret")))
    assert api.handle("GET", "/api/tasks", headers, None) == (500, {"error": "internal error"})
    assert records(api)[-1]["status"] == 500
    assert "secret" not in api.audit.read_text()


def test_audit_unavailable_prevents_submission(api, headers):
    api.audit.mkdir()
    assert api.handle("POST", "/api/requests", headers, {"request": "write"}) == (
        503, {"error": "service unavailable"})
    assert not api.queue.exists()


def test_concurrent_submissions_and_audit_lines(api, headers):
    with ThreadPoolExecutor(max_workers=4) as pool:
        ids = list(pool.map(lambda _: submit(api, headers), range(12)))
    assert len(set(ids)) == 12
    assert len([r for r in records(api) if r["status"] == 200]) == 12


def test_module_handle_uses_configured_api(api, headers, monkeypatch):
    monkeypatch.setattr(httpapi, "_default_api", api)
    assert httpapi.handle("GET", "/api/health", headers, None) == (200, {"ok": True})


def test_malformed_url_still_authenticates_first(api, headers):
    assert api.handle("GET", "http://[invalid", {}, None)[0] == 401
    assert api.handle("GET", "http://[invalid", headers, None)[0] == 404


def test_token_and_audit_symlinks_not_followed(api, headers, tmp_path):
    target = tmp_path / "outside"
    target.write_text(api.tokens.read_text())
    api.tokens.unlink()
    api.tokens.symlink_to(target)
    assert api.handle("GET", "/api/health", headers, None)[0] == 401
    api.audit.unlink()
    api.audit.symlink_to(target)
    before = target.read_bytes()
    assert api.handle("POST", "/api/requests", headers, {"request": "write"})[0] == 503
    assert target.read_bytes() == before and not api.queue.exists()


def test_consumer_lock_and_running_entries_never_replayed(api, headers):
    import fcntl

    key = submit(api, headers)
    with (api.queue / "consumer.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        assert api.process_next(lambda _: pytest.fail("consumer lock bypassed")) is None
    path = api.queue / (key + ".json")
    data = json.loads(path.read_text())
    data["status"] = "running"
    path.write_text(json.dumps(data))
    assert api.process_next(lambda _: pytest.fail("running entry replayed")) is None


def test_exactly_one_audit_record_per_request(api, headers):
    assert api.handle("GET", "/api/health", headers, None)[0] == 200
    assert len(records(api)) == 1


def test_ephemeral_token_is_process_local(api):
    token = secrets.token_urlsafe(32)
    api.add_ephemeral_token("internal", token)
    assert api.handle("GET", "/api/health", {"Authorization": "Bearer " + token}, b"")[0] == 200
    assert not (api.store.home / "tokens.json").exists()


def test_fixed_session_delegated_token_is_rejected(api):
    fixed = "internal-" + "0" * 8 + "session-delegated-" + "f" * 32
    assert api.handle("GET", "/api/health", {"Authorization": "Bearer " + fixed}, b"")[0] == 401
