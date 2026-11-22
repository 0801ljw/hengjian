import stat

from fusion.core import Store
from fusion.issue_policy import load, save
from fusion.issues import IssueStore


def test_policy_caps_issue_and_role(tmp_path):
    instance = tmp_path / "instance"
    instance.mkdir()
    saved = save(instance, {
        "max_tokens_per_issue": 5000,
        "max_calls_per_issue": 4,
        "role_max_tokens": {"gongbu": 2000},
        "role_max_calls": {"gongbu": 2},
        "monthly_tokens": 50000,
        "monthly_tokens_per_role": {"gongbu": 10000},
        "max_cost_usd_per_issue": 0.01,
        "role_max_cost_usd": {"gongbu": 0.02},
        "monthly_cost_usd": 1.5,
        "monthly_cost_usd_per_role": {"gongbu": 0.5},
    })
    assert saved["role_max_tokens"]["gongbu"] == 2000
    assert stat.S_IMODE((instance / "issue-policy.json").stat().st_mode) == 0o600
    assert load(instance)["max_tokens_per_issue"] == 5000
    assert load(instance)["monthly_tokens_per_role"]["gongbu"] == 10000
    assert load(instance)["monthly_cost_usd_per_role"]["gongbu"] == 0.5
    issues = IssueStore(Store(instance))
    gongbu = issues.create(title="g", assignee="gongbu", max_tokens=100000, max_calls=8)
    assert gongbu["max_tokens"] == 2000 and gongbu["max_calls"] == 2
    assert gongbu["max_cost_usd"] == 0.02
    codex = issues.create(title="c", assignee="codex", max_tokens=100000, max_calls=8)
    assert codex["max_tokens"] == 5000 and codex["max_calls"] == 4
    assert codex["max_cost_usd"] == 0.01
