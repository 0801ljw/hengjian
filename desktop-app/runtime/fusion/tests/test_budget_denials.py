"""1.1-3：预算拒绝必须可观测（独立表 + /api/budget 暴露），且跨事务回滚保留。"""
import json
from datetime import datetime, timedelta, timezone

import pytest

from fusion.budget import BudgetError, import_policy, ledger
from fusion.core import Store
from fusion.mutate import apply_mutation, payload_hash
from fusion.tests.board_fixture import envelope, keypair
from fusion.tests.test_fusion import doing


@pytest.fixture
def configured(tmp_path):
    private, public = keypair(tmp_path / "keys")
    store = Store(tmp_path / "data", public)

    def configure(company=100, agent=100, unpriced="block", revision=1):
        doc = {"id": f"policy-{revision}", "kind": "budget_policy", "principal": "board",
               "actor": "board:fixture", "created_at": datetime.now(timezone.utc).isoformat(),
               "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
               "revision": revision, "window": "calendar_month_utc", "company_cents": company,
               "agents": {"gongbu": agent}, "unpriced": unpriced}
        import_policy(store, envelope(doc, private))
        return doc

    configure()
    doing(store)
    return store, configure


def priced(store, task="T", estimate=60, **kwargs):
    return store.checkout(task, provider="priced-fixture", estimated_cents=estimate, **kwargs)


def test_limit_denial_is_recorded_and_survives_rollback(configured):
    store, configure = configured
    configure(company=50, agent=50, revision=2)

    with pytest.raises(BudgetError) as caught:
        priced(store, estimate=60)
    assert caught.value.code == "BUDGET_COMPANY_EXCEEDED"

    denials = ledger(store)["denials"]
    assert denials, "拒绝必须写进账本"
    latest = denials[0]
    assert latest["code"] == "BUDGET_COMPANY_EXCEEDED"
    assert latest["agent"] == "gongbu"
    assert latest["detail"]["limit_cents"] == 50
    assert latest["detail"]["estimated_cents"] == 60


def test_unpriced_block_denial_is_recorded(configured):
    store, configure = configured
    configure(unpriced="block", revision=2)

    with pytest.raises(BudgetError) as caught:
        store.checkout("T")
    assert caught.value.code == "UNPRICED_BLOCKED"

    codes = [d["code"] for d in ledger(store)["denials"]]
    assert "UNPRICED_BLOCKED" in codes


def test_successful_admission_writes_no_denial(configured):
    store, _ = configured
    priced(store, estimate=10)
    assert ledger(store)["denials"] == []
