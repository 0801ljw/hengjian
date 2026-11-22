"""The `block` mutation: narrow permission, audited transition, failure visibility."""
import pytest

from fusion.ai import DecisionClient
from fusion.ai_pipeline import Pipeline
from fusion.core import PolicyError, Store
from fusion.mutate import apply_mutation, payload_hash
from fusion.tests.test_ai import DISPATCH, GOOD, PLAN, ROUTE, FakeTransport


def _store(tmp_path):
    return Store(tmp_path / "db")


def _walk(store, task_id, path):
    store.create(task_id, "block fixture")
    for actor, state in path:
        store.move(task_id, actor, state, "fixture")


def _to_doing(store, task_id):
    _walk(store, task_id, [("taizi", "Zhongshu"), ("zhongshu", "Menxia"),
                           ("menxia", "Assigned"), ("shangshu", "Doing")])


def _block(store, task_id, *, actor="gongbu", target="Blocked"):
    task = store.get(task_id)
    return apply_mutation(store, task_id, actor, "block", target,
                          "execution failed: RuntimeError",
                          payload_sha256=payload_hash(task["payload"]),
                          expected_revision=task["revision"],
                          run_id="ai:test:failed",
                          fencing_token=task.get("fencing_token", 0))


def test_gongbu_blocks_own_doing_and_is_audited(tmp_path):
    store = _store(tmp_path)
    _to_doing(store, "T")
    result = _block(store, "T")
    assert result["state"] == "Blocked"
    assert result["flow_log"][-1]["from"] == "Doing"
    assert result["flow_log"][-1]["to"] == "Blocked"
    events = [event for event in store.snapshot()["events"]
              if event["task_id"] == "T" and event["action"] == "block"]
    assert events and events[-1]["detail"]["reason"].startswith("execution failed")


def test_block_stays_inside_the_frozen_permission(tmp_path):
    store = _store(tmp_path)
    _walk(store, "A", [("taizi", "Zhongshu"), ("zhongshu", "Menxia"),
                       ("menxia", "Assigned")])
    with pytest.raises(PolicyError):
        _block(store, "A")                       # Assigned belongs to shangshu
    _to_doing(store, "D")
    with pytest.raises(PolicyError):
        _block(store, "D", actor="shangshu")     # shangshu has no block command
    with pytest.raises(PolicyError):
        _block(store, "D", target="Done")        # block may only target Blocked
    with pytest.raises(PolicyError):
        _block(store, "D", target="Cancelled")   # high-risk cancellation is not block


def test_failed_run_blocks_the_task_instead_of_leaving_doing(tmp_path):
    store = Store(tmp_path / "store")
    transport = FakeTransport([ROUTE, PLAN, GOOD, DISPATCH, RuntimeError("provider boom")])
    client = DecisionClient(tmp_path / "audit", transport)
    pipeline = Pipeline(store, client, "T")
    # The AI client wraps transport failures in PolicyError; the run must not
    # stay in Doing regardless of the concrete error type.
    with pytest.raises(PolicyError):
        pipeline.run("write report.md", allow_unpriced=True)
    task = store.get("T")
    assert task["state"] == "Blocked"
    assert task["flow_log"][-1]["remark"] == "execution failed: PolicyError"
    assert store.snapshot()["runs"][0]["status"] == "failed"
    blocked = [event for event in store.snapshot()["events"] if event["action"] == "block"]
    assert blocked and blocked[-1]["detail"]["to"] == "Blocked"
