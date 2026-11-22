import pytest

from fusion.core import PolicyError, Store
from fusion.issues import IssueStore


def _store(tmp_path):
    store = Store(tmp_path / "instance")
    return store, IssueStore(store)


def test_claim_complete_review_approve(tmp_path):
    store, issues = _store(tmp_path)
    issue = issues.create(title="verify artifact", assignee="gongbu", reviewer="menxia",
                          approver="human", acceptance=["artifact exists"])
    claimed = issues.claim(issue["id"], "worker-1", lease_seconds=60)
    assert claimed["state"] == "running" and claimed["claim_token"]
    with pytest.raises(PolicyError):
        issues.claim(issue["id"], "worker-2")
    reviewing = issues.complete(issue["id"], claimed["claim_token"], "done", [{"path": "a.txt"}])
    assert reviewing["state"] == "review"
    approved = issues.review(issue["id"], "menxia", "approve", "looks good")
    assert approved["state"] == "review"
    final = issues.approve(issue["id"], "human")
    assert final["state"] == "done"
    assert final["result"]["summary"] == "done"


def test_blocker_releases_dependent(tmp_path):
    store, issues = _store(tmp_path)
    first = issues.create(title="first", assignee="gongbu")
    second = issues.create(title="second", assignee="gongbu", blockers=[first["id"]])
    assert second["state"] == "blocked"
    with pytest.raises(PolicyError):
        issues.claim(second["id"], "worker")
    claimed = issues.claim(first["id"], "worker")
    issues.complete(first["id"], claimed["claim_token"], "done")
    assert issues.get(second["id"])["state"] == "queued"
    assert issues.claim(second["id"], "worker")["state"] == "running"


def test_wakeup_coalescing_and_lease_recovery(tmp_path):
    store, issues = _store(tmp_path)
    issue = issues.create(title="wake", assignee="gongbu")
    assert [row["kind"] for row in issues.pending_wakeups()] == ["assigned"]
    issues.comment(issue["id"], "human", "first")
    issues.comment(issue["id"], "human", "second")
    kinds = [row["kind"] for row in issues.pending_wakeups()]
    assert kinds.count("commented") == 1
    claimed = issues.claim(issue["id"], "worker", lease_seconds=0)
    assert claimed["state"] == "running"
    assert issues.recover_expired() == 1
    recovered = issues.get(issue["id"])
    assert recovered["state"] == "queued"
    assert issues.claim(issue["id"], "worker-2")["state"] == "running"


def test_cancel_is_terminal_and_releases_dependents(tmp_path):
    store, issues = _store(tmp_path)
    first = issues.create(title="first", assignee="gongbu")
    second = issues.create(title="second", assignee="gongbu", blockers=[first["id"]])
    cancelled = issues.cancel(first["id"], "human", "no longer needed")
    assert cancelled["state"] == "cancelled"
    assert issues.get(second["id"])["state"] == "queued"
    with pytest.raises(PolicyError):
        issues.cancel(first["id"], "human")


def test_wakeup_claim_and_finish(tmp_path):
    store, issues = _store(tmp_path)
    issue = issues.create(title="wake claim", assignee="gongbu")
    wake = issues.claim_wakeup("worker")
    assert wake and wake["issue_id"] == issue["id"] and wake["kind"] == "assigned"
    assert issues.claim_wakeup("worker") is None
    issues.finish_wakeup(wake["id"])
    assert issues.pending_wakeups() == []


def test_approval_requires_reviewer_approval(tmp_path):
    store, issues = _store(tmp_path)
    issue = issues.create(title="gate", assignee="gongbu", reviewer="menxia", approver="human")
    claimed = issues.claim(issue["id"], "worker")
    issues.complete(issue["id"], claimed["claim_token"], "done")
    with pytest.raises(PolicyError):
        issues.approve(issue["id"], "human", admin=True)
    issues.review(issue["id"], "menxia", "approve")
    assert issues.approve(issue["id"], "human")["state"] == "done"


def test_fail_retries_with_backoff_until_max_attempts(tmp_path):
    store, issues = _store(tmp_path)
    issue = issues.create(title="retry", assignee="gongbu", max_attempts=2)
    first = issues.claim(issue["id"], "worker")
    retry = issues.fail(issue["id"], first["claim_token"], "network", retry=True)
    assert retry["state"] == "queued" and retry["attempts"] == 1
    assert retry["retry_at"]
    with pytest.raises(PolicyError):
        issues.claim(issue["id"], "worker")
    wake = issues.claim_wakeup("worker")
    issues.finish_wakeup(wake["id"])
    with store.connect() as conn:
        conn.execute("UPDATE issues SET retry_at='2000-01-01T00:00:00+00:00' WHERE id=?", (issue["id"],))
    assert issues.wake_due_retries() == 1
    second = issues.claim(issue["id"], "worker")
    blocked = issues.fail(issue["id"], second["claim_token"], "network", retry=True)
    assert blocked["state"] == "blocked" and blocked["attempts"] == 2
    assert blocked["result"]["will_retry"] is False


def test_delegate_blocks_parent_until_children_done(tmp_path):
    store, issues = _store(tmp_path)
    parent = issues.create(title="parent", assignee="codex")
    claimed = issues.claim(parent["id"], "worker")
    delegated = issues.delegate(parent["id"], claimed["claim_token"], [
        {"title": "child", "assignee": "pi", "body": "do it", "acceptance": ["done"]},
    ])
    assert delegated["state"] == "blocked" and delegated["children"]
    child = delegated["children"][0]
    assert child["state"] == "queued"
    child_claim = issues.claim(child["id"], "worker")
    issues.complete(child["id"], child_claim["claim_token"], "done")
    assert issues.get(parent["id"])["state"] == "queued"


def test_delegation_rejects_unknown_assignee(tmp_path):
    store, issues = _store(tmp_path)
    parent = issues.create(title="parent", assignee="codex")
    claimed = issues.claim(parent["id"], "worker")
    with pytest.raises(PolicyError):
        issues.delegate(parent["id"], claimed["claim_token"], [
            {"title": "child", "assignee": "not-an-agent", "body": "x"},
        ])


def test_monthly_role_budget_blocks_new_claims(tmp_path):
    from fusion.issue_policy import save
    store, issues = _store(tmp_path)
    save(store.home, {"monthly_tokens_per_role": {"gongbu": 10000}})
    first = issues.create(title="spend", assignee="gongbu", max_tokens=100000)
    claimed = issues.claim(first["id"], "worker")
    issues.complete(first["id"], claimed["claim_token"], "done",
                    usage={"total_tokens": 10000})
    second = issues.create(title="blocked", assignee="gongbu", max_tokens=100000)
    result = issues.claim(second["id"], "worker")
    assert result["state"] == "blocked"
    assert "monthly role budget" in result["result"]["error"]


def test_monthly_cost_cap_blocks_next_claim(tmp_path):
    from fusion.issue_policy import save
    store, issues = _store(tmp_path)
    first = issues.create(title="spend", assignee="gongbu")
    claimed = issues.claim(first["id"], "worker")
    issues.complete(first["id"], claimed["claim_token"], "done",
                    usage={"pricing": "configured", "cost_usd": 0.01, "total_tokens": 10})
    save(store.home, {"monthly_cost_usd_per_role": {"gongbu": 0.01}})
    second = issues.create(title="blocked", assignee="gongbu")
    result = issues.claim(second["id"], "worker")
    assert result["state"] == "blocked"
    assert "monthly role cost" in result["result"]["error"]


def test_monthly_cost_cap_fails_closed_on_unpriced_usage(tmp_path):
    from fusion.issue_policy import save
    store, issues = _store(tmp_path)
    first = issues.create(title="unknown", assignee="gongbu")
    claimed = issues.claim(first["id"], "worker")
    issues.complete(first["id"], claimed["claim_token"], "done",
                    usage={"pricing": "unpriced", "cost_usd": None, "total_tokens": 10})
    save(store.home, {"monthly_cost_usd": 1.0})
    second = issues.create(title="blocked", assignee="gongbu")
    result = issues.claim(second["id"], "worker")
    assert result["state"] == "blocked"
    assert "monthly company cost" in result["result"]["error"]


def test_failed_retry_writes_alert(tmp_path):
    from fusion.alerts import list_alerts
    store, issues = _store(tmp_path)
    issue = issues.create(title="retry", assignee="gongbu", max_attempts=1)
    claimed = issues.claim(issue["id"], "worker")
    issues.fail(issue["id"], claimed["claim_token"], "network", retry=True)
    alerts = list_alerts(store.home)
    assert any(alert["kind"] == "issue_retry_exhausted" and alert["ref"] == issue["id"]
               for alert in alerts)
