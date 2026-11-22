import pytest

from fusion.core import PolicyError, Store
from fusion.issue_executor import execute_issue, materialize_artifacts
from fusion.issues import IssueStore
from fusion.mutate import canonical
from fusion.tests.test_ai import FakeTransport


def _fixture(tmp_path):
    store = Store(tmp_path / "instance")
    issues = IssueStore(store)
    issue = issues.create(title="write artifact", assignee="codex", body="write a.txt")
    claimed = issues.claim(issue["id"], "worker")
    return store, issues, claimed


def _done(**overrides):
    value = {"status": "done", "summary": "wrote it", "blocker": "",
             "artifacts": [{"path": "a.txt", "content": "OK"}], "subtasks": []}
    value.update(overrides)
    return value


def test_execute_and_complete_issue(tmp_path):
    store, issues, claimed = _fixture(tmp_path)
    transport = FakeTransport([_done()])
    result = execute_issue(claimed, store.home, transport=transport)
    assert result["status"] == "done"
    assert result["artifacts"][0]["path"] == "a.txt"
    assert result["workspace"]
    assert (tmp_path / "instance" / "agents" / "codex" / "workspace").exists()
    completed = issues.complete(claimed["id"], claimed["claim_token"],
                                result["summary"], result["artifacts"])
    assert completed["state"] == "done"


def test_blocked_result_is_not_done(tmp_path):
    store, issues, claimed = _fixture(tmp_path)
    transport = FakeTransport([_done(status="blocked", blocker="missing input")])
    result = execute_issue(claimed, store.home, transport=transport)
    assert result["status"] == "blocked" and result["blocker"] == "missing input"


def test_artifact_traversal_is_refused(tmp_path):
    root = tmp_path / "run"
    with pytest.raises(PolicyError):
        materialize_artifacts(root, [{"path": "../escape", "content": "x"}])
    with pytest.raises(PolicyError):
        materialize_artifacts(root, [{"path": "/abs", "content": "x"}])


def test_acceptance_checks_pass_and_fail(tmp_path):
    store = Store(tmp_path / "instance")
    issues = IssueStore(store)
    issue = issues.create(title="checked", assignee="codex", checks=[
        {"type": "file_exists", "path": "a.txt"},
        {"type": "sha256", "path": "a.txt",
         "sha256": "565339bc4d33d72817b583024112eb7f5cdf3e5eef0252d6ec1b9c9a94e12bb3"},
    ])
    claimed = issues.claim(issue["id"], "worker")
    result = execute_issue(claimed, store.home, transport=FakeTransport([_done()]))
    assert result["status"] == "done" and all(item["ok"] for item in result["checks"])

    bad = issues.create(title="bad hash", assignee="codex", checks=[
        {"type": "sha256", "path": "a.txt", "sha256": "0" * 64},
    ])
    bad_claimed = issues.claim(bad["id"], "worker")
    blocked = execute_issue(bad_claimed, store.home, transport=FakeTransport([_done()]))
    assert blocked["status"] == "blocked" and "acceptance checks failed" in blocked["blocker"]


def test_delegate_result_is_returned(tmp_path):
    store, issues, claimed = _fixture(tmp_path)
    delegated = {
        "status": "delegate", "summary": "split", "blocker": "", "artifacts": [],
        "subtasks": [{"title": "child", "assignee": "pi", "body": "do it",
                      "acceptance": ["done"]}],
    }
    result = execute_issue(claimed, store.home, transport=FakeTransport([delegated]))
    assert result["status"] == "delegate" and result["subtasks"][0]["assignee"] == "pi"


def test_root_budget_exhaustion_blocks_claim(tmp_path):
    store = Store(tmp_path / "instance")
    issues = IssueStore(store)
    issue = issues.create(title="budget", assignee="codex", max_tokens=1000)
    with store.connect() as conn:
        conn.execute("UPDATE issues SET max_tokens=10 WHERE id=?", (issue["id"],))
    claimed = issues.claim(issue["id"], "worker")
    assert claimed["state"] == "blocked" and not claimed.get("claim_token")


def test_claim_reserves_root_budget_for_two_children(tmp_path):
    store = Store(tmp_path / "instance")
    issues = IssueStore(store)
    root = issues.create(title="root", assignee="codex", max_tokens=10000)
    first = issues.create(title="first", assignee="pi", parent_id=root["id"])
    second = issues.create(title="second", assignee="pi", parent_id=root["id"])
    one = issues.claim(first["id"], "w1")
    two = issues.claim(second["id"], "w2")
    assert one["budget_reserved"] > 0 and two["budget_reserved"] > 0
    assert one["budget_reserved"] + two["budget_reserved"] <= 10000


def test_executor_records_configured_cost(tmp_path):
    from fusion.pricing import save
    store = Store(tmp_path / "instance")
    save(store.home, {"models": {"gpt-6.1-sol": {
        "input_per_million_usd": 1, "output_per_million_usd": 2}}})
    issues = IssueStore(store)
    issue = issues.create(title="cost", assignee="codex")
    claimed = issues.claim(issue["id"], "worker")
    result = execute_issue(claimed, store.home, transport=FakeTransport([_done()]))
    assert result["usage"]["pricing"] == "configured"
    assert result["usage"]["cost_usd"] == 0.00002


def test_cost_cap_blocks_over_budget(tmp_path):
    from fusion.pricing import save
    store = Store(tmp_path / "instance")
    save(store.home, {"models": {"gpt-6.1-sol": {
        "input_per_million_usd": 1, "output_per_million_usd": 2}}})
    issues = IssueStore(store)
    issue = issues.create(title="cost", assignee="codex", max_cost_usd=0.00001)
    claimed = issues.claim(issue["id"], "worker")
    result = execute_issue(claimed, store.home, transport=FakeTransport([_done()]))
    assert result["status"] == "blocked" and "cost budget exceeded" in result["blocker"]


def test_cost_cap_with_unpriced_model_fails_closed(tmp_path):
    store = Store(tmp_path / "instance")
    issues = IssueStore(store)
    issue = issues.create(title="cost", assignee="codex", max_cost_usd=0.01)
    claimed = issues.claim(issue["id"], "worker")
    result = execute_issue(claimed, store.home, transport=FakeTransport([_done()]))
    assert result["status"] == "blocked" and "configured pricing" in result["blocker"]
