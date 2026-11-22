"""Instance policy for Issue budgets and worker limits.

The policy is local, 0600 JSON. It is a cap, never a grant: explicit Issue
requests can only be lowered, not raised above the instance/role ceiling.
"""
from __future__ import annotations

import json
from pathlib import Path

from .core import PolicyError

MAX_TOKENS = 10_000_000
MAX_CALLS = 8
DEFAULTS = {
    "max_tokens_per_issue": 100000,
    "max_calls_per_issue": 4,
    "role_max_tokens": {},
    "role_max_calls": {},
    "monthly_tokens": 0,
    "monthly_tokens_per_role": {},
    "max_cost_usd_per_issue": 0,
    "role_max_cost_usd": {},
    "monthly_cost_usd": 0,
    "monthly_cost_usd_per_role": {},
}


def _valid_int(value, low, high):
    return type(value) is int and low <= value <= high


def _clean(data):
    result = dict(DEFAULTS)
    if not isinstance(data, dict):
        return result
    if _valid_int(data.get("max_tokens_per_issue"), 1000, MAX_TOKENS):
        result["max_tokens_per_issue"] = data["max_tokens_per_issue"]
    if _valid_int(data.get("max_calls_per_issue"), 1, MAX_CALLS):
        result["max_calls_per_issue"] = data["max_calls_per_issue"]
    if _valid_int(data.get("monthly_tokens"), 0, MAX_TOKENS):
        result["monthly_tokens"] = data["monthly_tokens"]
    for key in ("max_cost_usd_per_issue", "monthly_cost_usd"):
        if isinstance(data.get(key), (int, float)) and 0 <= data[key] <= 1_000_000:
            result[key] = float(data[key])
    for key, low, high in (("role_max_tokens", 1000, MAX_TOKENS),
                           ("role_max_calls", 1, MAX_CALLS),
                           ("monthly_tokens_per_role", 0, MAX_TOKENS)):
        raw = data.get(key)
        if isinstance(raw, dict):
            cleaned = {}
            for role, value in raw.items():
                if isinstance(role, str) and len(role) <= 100 and _valid_int(value, low, high):
                    cleaned[role] = value
            result[key] = cleaned
    for key in ("role_max_cost_usd", "monthly_cost_usd_per_role"):
        raw = data.get(key)
        if isinstance(raw, dict):
            cleaned = {}
            for role, value in raw.items():
                if isinstance(role, str) and len(role) <= 100 \
                        and isinstance(value, (int, float)) and 0 <= value <= 1_000_000:
                    cleaned[role] = float(value)
            result[key] = cleaned
    return result


def load(instance):
    path = Path(instance) / "issue-policy.json"
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return dict(DEFAULTS)
    return _clean(data)


def save(instance, data):
    cleaned = _clean(data if isinstance(data, dict) else {})
    path = Path(instance) / "issue-policy.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(cleaned, ensure_ascii=False, indent=2))
    tmp.chmod(0o600)
    tmp.replace(path)
    return cleaned


def cap(instance, assignee, max_tokens, max_calls, max_cost_usd=None):
    policy = load(instance)
    tokens = min(int(max_tokens), int(policy["max_tokens_per_issue"]))
    calls = min(int(max_calls), int(policy["max_calls_per_issue"]))
    tokens = min(tokens, int(policy["role_max_tokens"].get(assignee, MAX_TOKENS)))
    calls = min(calls, int(policy["role_max_calls"].get(assignee, MAX_CALLS)))
    policy_cost = float(policy.get("role_max_cost_usd", {}).get(
        assignee, policy.get("max_cost_usd_per_issue", 0)) or 0)
    cost = policy_cost or (float(max_cost_usd) if max_cost_usd else 0)
    if max_cost_usd and policy_cost:
        cost = min(float(max_cost_usd), policy_cost)
    if tokens < 1000:
        raise PolicyError("policy leaves no token budget for this assignee")
    return tokens, calls, cost if cost > 0 else None
