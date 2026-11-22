"""Budget admission at the authoritative dispatch mutation (no model calls)."""
import copy
import json
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from fusion.budget import BudgetError, import_policy
from fusion.core import ROOT, Store, PolicyError, Conflict
from fusion.mutate import apply_mutation, payload_hash
from fusion.tests.board_fixture import keypair, envelope
from fusion.tests.test_fusion import doing


def test_unpriced_default_is_blocked(tmp_path):
    store = Store(tmp_path)
    doing(store)
    with pytest.raises(PolicyError, match="UNPRICED_BLOCKED"):
        store.checkout("T")


def test_unknown_is_not_zero(tmp_path):
    store = Store(tmp_path)
    doing(store)
    run = store.checkout("T", True)
    record = store.snapshot()["runs"][0]
    assert record["pricing"] == "unpriced"
    assert record["unknown_spend"] is True
    assert record["estimated_cents"] is None
    store.finish(run, {"status": "failed"})
    assert store.budget_status()["unknown_spend"] is True


def test_runtime_cannot_relabel_admission_metadata(tmp_path):
    store = Store(tmp_path)
    doing(store)
    run = store.checkout("T", True)
    with pytest.raises(PolicyError, match="admission metadata"):
        store.finish(run, {"status": "failed", "pricing": "priced", "estimated_cents": 0})
    store.finish(run, {"status": "failed", "actual_cents": 0, "cost_usd": 0})
    record = store.snapshot()["runs"][0]
    assert record["cost_usd"] is None and record["actual_cents"] is None


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
        signed = envelope(doc, private)
        import_policy(store, signed)
        return signed
    configure()
    doing(store)
    return store, configure, private


def priced(store, task="T", estimate=60, **kwargs):
    return store.checkout(task, provider="priced-fixture", estimated_cents=estimate, **kwargs)


def add_task(store, task_id):
    store.create(task_id, "fixture")
    for actor, target in (("taizi", "Zhongshu"), ("zhongshu", "Menxia"),
                          ("menxia", "Assigned"), ("shangshu", "Doing")):
        store.move(task_id, actor, target, "fixture")


@pytest.mark.parametrize("company,agent,code", [
    (100, 50, "BUDGET_AGENT_EXCEEDED"), (50, 100, "BUDGET_COMPANY_EXCEEDED"),
    (50, 50, "BUDGET_COMPANY_EXCEEDED"), (100, 100, None)])
def test_dual_limits_four_combinations(configured, company, agent, code):
    store, configure, _ = configured
    configure(company, agent, revision=2)
    before = store.snapshot()
    if code:
        with pytest.raises(BudgetError) as caught:
            priced(store)
        assert caught.value.code == code
        assert store.snapshot() == before
        assert store.budget_status()["company"]["reserved_cents"] == 0
    else:
        priced(store)
        assert store.budget_status()["company"]["reserved_cents"] == 60
        assert store.budget_status()["agent"]["reserved_cents"] == 60


def test_warn_eighty_and_stop_at_full(configured):
    store, _, _ = configured
    first = priced(store, estimate=80)
    with store.connect() as conn:
        assert conn.execute("SELECT count(*) FROM budget_events WHERE kind='warn'").fetchone()[0] == 2
    store.finish(first, {"status": "failed", "actual_cents": 80})
    second = priced(store, estimate=20)  # equality is permitted by spent + estimate <= cap
    store.finish(second, {"status": "failed", "actual_cents": 20})
    with pytest.raises(BudgetError, match="BUDGET_COMPANY_EXCEEDED"):
        priced(store, estimate=0)


def test_unpriced_board_policy_cannot_be_overridden_by_flag(configured):
    store, configure, _ = configured
    with pytest.raises(BudgetError, match="UNPRICED_BLOCKED"):
        store.checkout("T", True, estimated_cents=0)
    configure(unpriced="allow", revision=2)
    run = store.checkout("T")
    record = store.snapshot()["runs"][0]
    assert record["unknown_spend"] is True and record["estimated_cents"] is None
    assert store.budget_status()["company"]["reserved_cents"] == 0
    with store.connect() as conn:
        assert conn.execute("SELECT count(*) FROM budget_events WHERE kind='unpriced'").fetchone()[0] == 1
    store.finish(run, {"status": "failed", "actual_cents": 0})
    assert store.snapshot()["runs"][0]["actual_cents"] is None
    assert store.budget_status()["unknown_spend"] is True


def test_reserve_settle_refund_and_overrun(configured):
    store, _, _ = configured
    run = priced(store, estimate=90)
    store.finish(run, {"status": "failed", "actual_cents": 30})
    state = store.budget_status()["company"]
    assert state == {"known_spend_cents": 30, "reserved_cents": 0, "unknown_spend": False}
    run = priced(store, estimate=70)
    store.finish(run, {"status": "timeout", "actual_cents": 80})
    assert store.budget_status()["company"]["known_spend_cents"] == 110
    with pytest.raises(BudgetError):
        priced(store, estimate=0)
    with pytest.raises(Conflict):
        store.finish(run, {"status": "failed", "actual_cents": 0})


def test_missing_meter_is_unknown_not_free(configured):
    store, _, _ = configured
    run = priced(store)
    store.finish(run, {"status": "unavailable"})
    assert store.snapshot()["runs"][0]["actual_cents"] is None
    assert store.budget_status()["unknown_spend"] is True
    assert store.budget_status()["company"]["reserved_cents"] == 0


@pytest.mark.parametrize("value", [-1, 1.2, True, "10", 2**63])
def test_integer_only(configured, value):
    store, configure, _ = configured
    with pytest.raises(PolicyError, match="integer"):
        priced(store, estimate=value)
    with pytest.raises(PolicyError, match="integer"):
        configure(company=value, revision=2)
    run = priced(store)
    with pytest.raises(PolicyError, match="integer"):
        store.finish(run, {"status": "failed", "actual_cents": value})
    assert store.snapshot()["runs"][0]["status"] == "running"


def test_two_concurrent_reservations_cannot_overspend(configured):
    store, _, _ = configured
    add_task(store, "U")
    barrier = threading.Barrier(2)
    def attempt(task):
        other = Store(store.home)
        barrier.wait(timeout=5)
        try:
            priced(other, task)
            return "ok"
        except BudgetError:
            return "denied"
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(attempt, ["T", "U"])) == ["denied", "ok"]
    assert store.budget_status()["company"]["reserved_cents"] == 60
    assert len(store.snapshot()["runs"]) == 1


def test_real_process_crash_reclaims_on_startup(configured):
    store, _, _ = configured
    code = """
import os, sys
from pathlib import Path
from fusion.core import Store
s = Store(Path(sys.argv[1]))
s.checkout('T', provider='priced-fixture', estimated_cents=90)
os._exit(23)
"""
    process = subprocess.run([sys.executable, "-c", code, str(store.home)], cwd=ROOT,
                             capture_output=True, text=True)
    assert process.returncode == 23, process.stderr
    assert store.budget_status()["company"]["reserved_cents"] == 90
    restarted = Store(store.home)
    assert restarted.budget_status()["company"]["reserved_cents"] == 0
    assert restarted.budget_status()["unknown_spend"] is True
    orphan = restarted.snapshot()["runs"][0]
    assert orphan["status"] == "orphaned"
    with pytest.raises(Conflict):
        restarted.finish(orphan["id"], {"status": "failed", "actual_cents": 0})
    priced(restarted, estimate=100)
    assert Store(store.home).budget_status()["company"]["reserved_cents"] == 100
    with store.connect() as conn:
        assert conn.execute("SELECT count(*) FROM budget_events WHERE kind='orphaned'").fetchone()[0] == 1


def test_utc_calendar_month_and_late_settlement(configured, monkeypatch):
    import fusion.budget as budget
    store, _, _ = configured
    monkeypatch.setattr(budget, "month", lambda: "2026-12")
    run = priced(store, estimate=100)
    monkeypatch.setattr(budget, "month", lambda: "2027-01")
    assert store.budget_status()["company"]["reserved_cents"] == 0
    store.finish(run, {"status": "failed", "actual_cents": 20})
    assert store.budget_status(month="2026-12")["company"]["known_spend_cents"] == 20
    assert store.budget_status()["company"]["known_spend_cents"] == 0


def test_admission_freezes_month_once_at_utc_rollover(configured, monkeypatch):
    import fusion.budget as budget
    store, _, _ = configured
    calls = []
    def clock():
        calls.append(1)
        return "2026-12" if len(calls) == 1 else "2027-01"
    monkeypatch.setattr(budget, "month", clock)
    priced(store, estimate=100)
    assert len(calls) == 1
    assert store.budget_status(month="2026-12")["company"]["reserved_cents"] == 100
    assert store.budget_status(month="2027-01")["company"]["reserved_cents"] == 0


def test_direct_mutation_cannot_bypass_budget_or_use_task_approval(configured):
    store, _, _ = configured
    task = store.get("T")
    args = dict(task_id="T", actor="shangshu", command="dispatch", target="Doing", reason="dispatch",
                run_id="direct", expected_revision=task["revision"], payload_sha256=payload_hash(task["payload"]),
                payload={"provider": "priced-fixture", "estimated_cents": 101})
    with pytest.raises(BudgetError):
        apply_mutation(store, **args)
    with pytest.raises(PolicyError, match="cannot bypass"):
        apply_mutation(store, **args, approval_id="task-approval")
    with store.transaction() as conn:
        with pytest.raises(BudgetError):
            apply_mutation(store, **args, conn=conn)
        # Even a caller catching an error and committing cannot retain a partial run.
        assert conn.execute("SELECT count(*) FROM runs").fetchone()[0] == 0
    assert store.snapshot()["runs"] == []


def test_signed_policy_revision_and_signature_required(configured):
    store, configure, _ = configured
    signed = configure(company=150, revision=2)
    assert import_policy(store, signed) == signed["document"]
    forged = copy.deepcopy(signed)
    forged["document"]["company_cents"] = 10000
    with pytest.raises(PolicyError, match="signature"):
        import_policy(store, forged)
    with pytest.raises(Conflict, match="stale"):
        configure(company=200, revision=1)
    assert priced(store, estimate=100)


def test_one_shot_signed_override_is_bound_and_audited(configured):
    from fusion.budget import month
    store, _, private = configured
    doc = {"id": "special", "kind": "budget_override", "principal": "board", "actor": "board:fixture",
           "created_at": datetime.now(timezone.utc).isoformat(),
           "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
           "run_id": "special-run", "task_id": "T", "month": month(), "estimated_cents": 120,
           "company_extra_cents": 20, "agent_extra_cents": 20}
    signed = envelope(doc, private)
    with pytest.raises(PolicyError, match="binding"):
        priced(store, estimate=120, run_id="wrong-run", budget_override=signed)
    run = priced(store, estimate=120, run_id="special-run", budget_override=signed)
    with store.connect() as conn:
        assert conn.execute("SELECT count(*) FROM budget_events WHERE kind='override'").fetchone()[0] == 1
    store.finish(run, {"status": "failed", "actual_cents": 120})
    with pytest.raises(BudgetError):
        priced(store, estimate=0)
    with pytest.raises(Conflict):
        priced(store, estimate=120, run_id="special-run", budget_override=signed)


def test_priced_without_both_caps_fails_closed(tmp_path):
    store = Store(tmp_path)
    doing(store)
    with pytest.raises(BudgetError, match="BUDGET_NOT_CONFIGURED"):
        priced(store)


def test_unpriced_allow_does_not_bypass_exhausted_budget(configured):
    store, configure, _ = configured
    configure(unpriced="allow", revision=2)
    run = priced(store, estimate=100)
    store.finish(run, {"status": "failed", "actual_cents": 100})
    with pytest.raises(BudgetError, match="BUDGET_COMPANY_EXCEEDED"):
        store.checkout("T", True)


def test_output_failure_rolls_back_settlement(configured):
    store, _, _ = configured
    run = priced(store)
    before = store.snapshot()
    with pytest.raises(KeyError):
        store.finish(run, {"status": "succeeded", "actual_cents": 10})
    assert store.snapshot() == before
    assert store.budget_status()["company"]["reserved_cents"] == 60


def test_startup_does_not_reclaim_other_live_process(configured):
    store, _, _ = configured
    code = """
import sys
from pathlib import Path
from fusion.core import Store
s = Store(Path(sys.argv[1]))
s.checkout('T', provider='priced-fixture', estimated_cents=90)
print('reserved', flush=True)
sys.stdin.readline()
"""
    child = subprocess.Popen([sys.executable, "-c", code, str(store.home)], cwd=ROOT,
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "reserved"
        restarted = Store(store.home)
        assert restarted.budget_status()["company"]["reserved_cents"] == 90
        assert restarted.snapshot()["runs"][0]["status"] == "running"
    finally:
        child.kill()
        child.communicate(timeout=5)
    assert Store(store.home).budget_status()["company"]["reserved_cents"] == 0


def test_cli_ledger_persists_unknown_and_policy_history(configured):
    store, _, _ = configured
    run = priced(store)
    store.finish(run, {"status": "failed", "actual_cents": 12})
    command = [sys.executable, "-m", "fusion.control", "--home", str(store.home), "budget-ledger"]
    result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["status"]["company"]["known_spend_cents"] == 12
    assert data["reservations"][0]["status"] == "settled"
    assert {e["kind"] for e in data["events"]} >= {"policy_changed", "reserved", "settled"}


@pytest.mark.parametrize("extra", ["company_extra_cents", "agent_extra_cents"])
def test_override_must_cover_both_layers(configured, extra):
    from fusion.budget import month
    store, _, private = configured
    doc = {"id": "insufficient", "kind": "budget_override", "principal": "board", "actor": "board:fixture",
           "created_at": datetime.now(timezone.utc).isoformat(),
           "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
           "run_id": "R", "task_id": "T", "month": month(), "estimated_cents": 120,
           "company_extra_cents": 20, "agent_extra_cents": 20}
    doc[extra] = 0
    with pytest.raises(BudgetError):
        priced(store, estimate=120, run_id="R", budget_override=envelope(doc, private))
    with store.connect() as conn:
        assert conn.execute("SELECT count(*) FROM budget_overrides").fetchone()[0] == 0
    assert store.snapshot()["runs"] == []


def test_unpriced_has_monthly_dispatch_cap(configured, monkeypatch):
    store, configure, _ = configured
    configure(unpriced="allow", revision=2)
    monkeypatch.setenv("DUTANG_UNPRICED_MONTHLY_MAX", "1")
    monkeypatch.setenv("DUTANG_UNPRICED_AGENT_MAX", "1")
    run = store.checkout("T")
    store.finish(run, {"status": "failed", "actual_cents": 0})
    add_task(store, "T2")
    with pytest.raises(BudgetError, match="UNPRICED_LIMIT_EXCEEDED"):
        store.checkout("T2")
    with store.connect() as conn:
        denials = conn.execute(
            "SELECT count(*) FROM budget_denials WHERE code='UNPRICED_LIMIT_EXCEEDED'").fetchone()[0]
    assert denials == 1
