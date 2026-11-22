"""Offline decision fixtures exercise control flow; real evidence is ai_demo only."""
import copy
import gc
import hashlib
import json
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from fusion.ai import DecisionClient, MODEL, StreamTransport, strict_json
from fusion.ai_pipeline import Pipeline, write_files
from fusion.ai_schema import SCHEMAS, check_order, check_plan, validate
from fusion.budget import import_policy
from fusion.core import PolicyError, Store
from fusion.mutate import canonical, payload_hash
from fusion.tests.board_fixture import envelope, keypair

ROUTE = {"kind": "task", "title": "Local report", "requirement": "write report.md", "reason": "concrete file task"}
PLAN = {"goal": "write report.md", "subtasks": [{"id": "write", "assignee": "gongbu",
        "instruction": "write report.md", "depends_on": [], "acceptance": ["report.md is nonempty"]}]}
GOOD = {"verdict": "approve", "reasons": ["bounded and testable"], "required_changes": []}
BAD = {"verdict": "reject", "reasons": ["unsafe"], "required_changes": ["remove network access"]}
DISPATCH = {"order": ["write"], "reason": "no dependencies"}
WORK = {"files": [{"path": "report.md", "content": "Real file contents\n"}], "report": "generated text"}


class FakeTransport:
    def __init__(self, values):
        self.values = iter(values)
        self.requests = []

    def __call__(self, request, path):
        self.requests.append(request)
        value = next(self.values)
        if isinstance(value, Exception):
            raise value
        return {"output": canonical(value) if not isinstance(value, str) else value,
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
                "http_status": 200, "elapsed_sec": 0.1, "model": MODEL}


def setup(tmp_path, values, **kwargs):
    transport = FakeTransport(values)
    client = DecisionClient(tmp_path / "audit", transport, **kwargs)
    store = Store(tmp_path / "store")
    return Pipeline(store, client, "T"), client, transport


def test_chat_creates_no_task(tmp_path):
    pipeline, client, _ = setup(tmp_path, [{**ROUTE, "kind": "chat"}])
    assert pipeline.run("hello")["status"] == "chat"
    assert pipeline.store.snapshot()["tasks"] == []
    assert client.calls == 1


def test_task_real_pipeline_gate_and_files(tmp_path):
    pipeline, client, transport = setup(tmp_path, [ROUTE, PLAN, GOOD, DISPATCH, WORK])
    result = pipeline.run("write report.md", allow_unpriced=True)
    assert result["status"] == "awaiting_board"
    assert pipeline.store.get("T")["state"] == "Review"
    assert pipeline.store.snapshot()["runs"][0]["status"] == "succeeded"
    assert result["files"][0]["sha256"] == hashlib.sha256(b"Real file contents\n").hexdigest()
    assert client.calls == 5
    assert {request["reasoning_effort"] for request in transport.requests} == {"low"}
    assert all(request["stream"] and request["response_format"]["json_schema"]["strict"] for request in transport.requests)
    before = pipeline.store.snapshot()
    with pytest.raises(PolicyError, match="approval"):
        pipeline.mutate("shangshu", "state", "Done", "AI says approval unnecessary")
    assert pipeline.store.snapshot() == before
    assert not any(record["kind"] == "approval" for record in before["decisions"])


@pytest.mark.parametrize("invalid", [{}, {**PLAN, "illegal": True}, "not JSON",
                                    '{"goal":"x","goal":"y","subtasks":[]}'])
def test_plan_schema_retries_once(tmp_path, invalid):
    _, client, transport = setup(tmp_path, [invalid, PLAN])
    assert client.decide("zhongshu", {}) == PLAN
    assert len(transport.requests) == 2
    records = client.summary()["steps"]
    assert [record["status"] for record in records] == ["invalid", "valid"]
    assert "validation_error" in transport.requests[1]["messages"][1]["content"]


@pytest.mark.parametrize("step", list(SCHEMAS))
def test_each_schema_failure_is_explicit_after_one_retry(tmp_path, step):
    _, client, _ = setup(tmp_path, [{}, {}])
    with pytest.raises(PolicyError, match="after 2 attempts"):
        client.decide(step, {})
    assert client.calls == 2


def test_reject_then_replan_then_approve(tmp_path):
    pipeline, client, transport = setup(tmp_path, [ROUTE, PLAN, BAD, PLAN, GOOD, DISPATCH, WORK])
    assert pipeline.run("write", allow_unpriced=True)["status"] == "awaiting_board"
    reviews = [d for d in pipeline.store.snapshot()["decisions"] if d["kind"] == "review_decision"]
    assert len(reviews) == 2
    assert client.calls == 7
    assert "remove network access" in transport.requests[3]["messages"][1]["content"]
    flow = pipeline.store.get("T")["flow_log"]
    assert any(entry["from"] == "Menxia" and entry["to"] == "Zhongshu" for entry in flow)


def test_rejection_loop_two_replans_then_terminal(tmp_path):
    pipeline, client, _ = setup(tmp_path, [ROUTE] + [PLAN, BAD] * 3)
    result = pipeline.run("write", allow_unpriced=True)
    assert result["status"] == "rejected" and result["replans"] == 2
    assert pipeline.store.get("T")["state"] == "Cancelled"
    assert pipeline.store.snapshot()["runs"] == []
    assert client.calls == 7


def test_audit_hashes_are_reproducible_and_unpriced_unknown(tmp_path):
    _, client, _ = setup(tmp_path, [ROUTE])
    client.decide("taizi", {})
    record = client.summary()["steps"][0]
    assert record["model_sha256"] == payload_hash(MODEL)
    request = strict_json((client.directory / record["request_path"]).read_text())
    assert record["prompt_sha256"] == payload_hash(request)
    assert record["output_sha256"] == hashlib.sha256((client.directory / record["output_path"]).read_bytes()).hexdigest()
    assert record["decision"] == ROUTE and record["tokens"]["total_tokens"] == 15
    assert record["at"] and record["http_status"] == 200
    assert record["cost_usd"] is None and client.summary()["total_cost_usd"] is None


def test_transport_failure_never_downgrades_or_retries(tmp_path):
    _, client, transport = setup(tmp_path, [OSError("offline"), ROUTE])
    with pytest.raises(PolicyError, match="no fallback"):
        client.decide("taizi", {})
    assert len(transport.requests) == 1
    assert client.summary()["steps"][0]["status"] == "transport_error"


def test_round_limit_counts_invalid_and_failed_attempts(tmp_path):
    _, client, _ = setup(tmp_path, [{}, ROUTE, ROUTE], max_calls=2)
    client.decide("taizi", {})
    with pytest.raises(PolicyError, match="MODEL_CALL_LIMIT"):
        client.decide("taizi", {})
    with pytest.raises(FileExistsError):
        DecisionClient(client.directory, FakeTransport([]))


def test_default_unpriced_budget_refuses_before_worker_call(tmp_path):
    pipeline, client, _ = setup(tmp_path, [ROUTE, PLAN, GOOD, DISPATCH, WORK])
    with pytest.raises(PolicyError, match="UNPRICED_BLOCKED"):
        pipeline.run("write")
    assert client.calls == 4
    assert pipeline.store.snapshot()["runs"] == []


def test_exhausted_budget_refuses_dispatch_even_with_allow_flag(tmp_path):
    pipeline, client, _ = setup(tmp_path, [ROUTE, PLAN, GOOD, DISPATCH, WORK])
    private, public = keypair(tmp_path / "keys")
    pipeline.store.board_key_file = public
    doc = {"id": "policy", "kind": "budget_policy", "principal": "board", "actor": "board:fixture",
           "created_at": datetime.now(timezone.utc).isoformat(),
           "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
           "revision": 1, "window": "calendar_month_utc", "company_cents": 0,
           "agents": {"gongbu": 0}, "unpriced": "allow"}
    import_policy(pipeline.store, envelope(doc, private))
    with pytest.raises(PolicyError, match="BUDGET_COMPANY_EXCEEDED"):
        pipeline.run("write", allow_unpriced=True)
    assert client.calls == 4 and not pipeline.store.snapshot()["runs"]


@pytest.mark.parametrize("path", ["../outside.md", "/tmp/out.md", "a/../../bad", ".env",
                                  "manifest.json", "board-request.json", "a/../b"])
def test_worker_paths_confined(tmp_path, path):
    with pytest.raises(PolicyError):
        write_files(tmp_path, [{"path": path, "content": "bad"}])


def test_worker_cannot_follow_symlink_or_overwrite(tmp_path):
    (tmp_path / "link").symlink_to(tmp_path.parent, target_is_directory=True)
    with pytest.raises(PolicyError):
        write_files(tmp_path, [{"path": "link/escape.md", "content": "x"}])
    write_files(tmp_path, WORK["files"])
    with pytest.raises(PolicyError):
        write_files(tmp_path, WORK["files"])


def test_worker_error_settles_and_releases_lease(tmp_path):
    pipeline, _, _ = setup(tmp_path, [ROUTE, PLAN, GOOD, DISPATCH, OSError("offline")])
    with pytest.raises(PolicyError):
        pipeline.run("write", allow_unpriced=True)
    assert pipeline.store.snapshot()["runs"][0]["status"] == "failed"
    with pipeline.store.connect() as conn:
        assert conn.execute("SELECT owner_id FROM leases").fetchone() == (None,)


def test_dependency_cycle_and_order_rejected():
    plan = copy.deepcopy(PLAN)
    plan["subtasks"][0]["depends_on"] = ["write"]
    with pytest.raises(PolicyError, match="cycle"):
        check_plan(plan)
    plan["subtasks"][0]["depends_on"] = ["unknown"]
    with pytest.raises(PolicyError, match="unknown"):
        check_plan(plan)
    for order in [[], ["unknown"], ["write", "write"]]:
        with pytest.raises(PolicyError):
            check_order(order, PLAN)


def test_invalid_dispatch_order_retries_once(tmp_path):
    _, client, _ = setup(tmp_path, [{**DISPATCH, "order": ["unknown"]}, DISPATCH])
    assert client.decide("shangshu", {"plan": PLAN}) == DISPATCH
    assert client.calls == 2


def test_dependency_order_for_multiple_tasks():
    plan = copy.deepcopy(PLAN)
    plan["subtasks"].append({**copy.deepcopy(PLAN["subtasks"][0]), "id": "verify", "depends_on": ["write"]})
    check_plan(plan)
    check_order(["write", "verify"], plan)
    with pytest.raises(PolicyError, match="dependency"):
        check_order(["verify", "write"], plan)


def test_lease_admission_failure_never_calls_worker(tmp_path, monkeypatch):
    import fusion.leases as leases
    pipeline, client, _ = setup(tmp_path, [ROUTE, PLAN, GOOD, DISPATCH, WORK])
    def unavailable(*args):
        raise leases.LeaseError("LEASE_BUSY")
    monkeypatch.setattr(leases, "acquire", unavailable)
    with pytest.raises(PolicyError, match="LEASE_BUSY"):
        pipeline.run("write", allow_unpriced=True)
    assert client.calls == 4 and not pipeline.store.snapshot()["runs"]


def test_prompt_size_limit_before_network(tmp_path):
    _, client, transport = setup(tmp_path, [ROUTE])
    with pytest.raises(PolicyError, match="MODEL_INPUT_LIMIT"):
        client.decide("taizi", {"request": "x" * 60000})
    assert client.calls == 0 and not transport.requests


def test_resume_preserves_durable_round_limit(tmp_path):
    client = DecisionClient(tmp_path / "audit", FakeTransport([ROUTE]), max_calls=2)
    client.decide("taizi", {})
    del client
    gc.collect()
    resumed = DecisionClient.resume(tmp_path / "audit", FakeTransport([ROUTE]))
    assert resumed.calls == 1 and resumed.max_calls == 2
    resumed.decide("taizi", {})
    with pytest.raises(PolicyError, match="MODEL_CALL_LIMIT"):
        resumed.decide("taizi", {})
    assert resumed.summary()["total_tokens"] == 30


def test_round_cannot_have_two_controllers(tmp_path):
    client = DecisionClient(tmp_path / "audit", FakeTransport([]))
    with pytest.raises(PolicyError, match="active controller"):
        DecisionClient.resume(tmp_path / "audit", FakeTransport([]))
    assert client.calls == 0


@pytest.mark.parametrize("complete,finish,usage,expected", [
    (True, "stop", True, True), (False, "stop", True, False),
    (True, "length", True, False), (True, "stop", False, True)])
def test_real_transport_sse_contract(tmp_path, complete, finish, usage, expected):
    received = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            received.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            events = [{"model": MODEL, "choices": [{"index": 0, "delta": {"content": canonical(ROUTE)},
                                                      "finish_reason": finish}]}]
            if usage:
                events.append({"choices": [], "usage": {"total_tokens": 15}})
            for event in events:
                self.wfile.write(("data: " + canonical(event) + "\r\n\r\n").encode())
            if complete:
                self.wfile.write(b"data: [DONE]\r\n\r\n")
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        transport = StreamTransport.__new__(StreamTransport)
        transport.endpoint = f"http://127.0.0.1:{server.server_port}/chat/completions"
        transport.key = "TEST-NOT-A-SECRET"
        client = DecisionClient(tmp_path / "audit", transport)
        if expected:
            assert client.decide("taizi", {}) == ROUTE
            assert (client.summary()["steps"][0]["tokens"] is None) == (not usage)
        else:
            with pytest.raises(PolicyError):
                client.decide("taizi", {})
        assert received[0]["stream"] and received[0]["reasoning_effort"] == "low"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
