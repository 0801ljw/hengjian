"""D1/D2: cancellation paths end to end, and they stay narrow.

Doing -> Cancelled : gongbu requests, board signs, shangshu confirms.
Menxia -> Cancelled: menxia requests, board signs, zhongshu confirms.
"""
import subprocess
from datetime import datetime, timedelta, timezone

import pytest

from fusion.approvals import approval_document, import_approval, prepare_request
from fusion.core import PolicyError, Store
from fusion.mutate import apply_mutation, payload_hash
from fusion.tests.board_fixture import envelope, keypair


def _store(tmp_path):
    priv, pub = keypair(tmp_path / "keys")
    return Store(tmp_path / "db", pub), priv


def _walk(store, task_id, path):
    store.create(task_id, "取消路径验证")
    for actor, state in path:
        store.move(task_id, actor, state, "fixture")


def _sign(store, request, priv):
    doc = approval_document(request, actor="board:test", run_id="human-1",
                            expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat())
    return import_approval(store, envelope(doc, priv))


@pytest.fixture
def doing_task(tmp_path):
    store, priv = _store(tmp_path)
    _walk(store, "T", [("taizi", "Zhongshu"), ("zhongshu", "Menxia"),
                       ("menxia", "Assigned"), ("shangshu", "Doing")])
    return store, priv


@pytest.fixture
def menxia_task(tmp_path):
    store, priv = _store(tmp_path)
    _walk(store, "T", [("taizi", "Zhongshu"), ("zhongshu", "Menxia")])
    return store, priv


def test_gongbu_can_request_cancellation_and_shangshu_confirms(doing_task):
    store, priv = doing_task
    task = store.get("T")
    request = prepare_request(store, "T", "gongbu", "Cancelled", "现场条件不具备",
                              run_id="run-1", expected_revision=task["revision"],
                              payload_sha256=payload_hash(task["payload"]), command="cancel")
    approval = _sign(store, request, priv)
    apply_mutation(store, "T", "gongbu", "cancel", "Cancelled", "现场条件不具备",
                   payload_sha256=payload_hash(task["payload"]), run_id="run-1",
                   expected_revision=task["revision"], approval_id=approval["id"])
    pending = store.get("T")
    assert pending["state"] == "PendingConfirm"
    assert pending["pending_confirm"]["confirm_by"] == "shangshu"

    apply_mutation(store, "T", "shangshu", "confirm", "Cancelled", "同意取消",
                   payload_sha256=payload_hash(store.get("T")["payload"]), run_id="run-1-confirm",
                   expected_revision=store.get("T")["revision"])
    assert store.get("T")["state"] == "Cancelled"


def test_menxia_can_request_cancellation_and_zhongshu_confirms(menxia_task):
    store, priv = menxia_task
    task = store.get("T")
    request = prepare_request(store, "T", "menxia", "Cancelled", "需求撤回",
                              run_id="run-2", expected_revision=task["revision"],
                              payload_sha256=payload_hash(task["payload"]))
    approval = _sign(store, request, priv)
    apply_mutation(store, "T", "menxia", "state", "Cancelled", "需求撤回",
                   payload_sha256=payload_hash(task["payload"]), run_id="run-2",
                   expected_revision=task["revision"], approval_id=approval["id"])
    pending = store.get("T")
    assert pending["state"] == "PendingConfirm"
    assert pending["pending_confirm"]["confirm_by"] == "zhongshu"

    apply_mutation(store, "T", "zhongshu", "confirm", "Cancelled", "同意",
                   payload_sha256=payload_hash(store.get("T")["payload"]), run_id="run-2-confirm",
                   expected_revision=store.get("T")["revision"])
    assert store.get("T")["state"] == "Cancelled"


def test_cancel_cannot_target_anything_but_cancelled(doing_task):
    store, priv = doing_task
    task = store.get("T")
    with pytest.raises(PolicyError):
        prepare_request(store, "T", "gongbu", "Done", "想直接完工", run_id="run-3",
                        expected_revision=task["revision"],
                        payload_sha256=payload_hash(task["payload"]), command="cancel")
    with pytest.raises(PolicyError):
        apply_mutation(store, "T", "gongbu", "cancel", "Done", "想直接完工",
                       payload_sha256=payload_hash(task["payload"]), run_id="run-3",
                       expected_revision=task["revision"])


def test_other_roles_cannot_use_cancel(doing_task):
    store, _ = doing_task
    task = store.get("T")
    for actor in ("menxia", "zhongshu", "taizi"):
        with pytest.raises(PolicyError):
            apply_mutation(store, "T", actor, "cancel", "Cancelled", "越权",
                           payload_sha256=payload_hash(task["payload"]), run_id="run-4",
                           expected_revision=task["revision"])


def test_cancel_still_requires_board_approval(doing_task):
    store, _ = doing_task
    task = store.get("T")
    with pytest.raises(PolicyError):
        apply_mutation(store, "T", "gongbu", "cancel", "Cancelled", "没签名",
                       payload_sha256=payload_hash(task["payload"]), run_id="run-5",
                       expected_revision=task["revision"])


def test_gongbu_gains_no_general_state_command(doing_task):
    """The grant must not let the executor route its own work to Done."""
    store, _ = doing_task
    task = store.get("T")
    with pytest.raises(PolicyError):
        apply_mutation(store, "T", "gongbu", "state", "Done", "自己批",
                       payload_sha256=payload_hash(task["payload"]), run_id="run-6",
                       expected_revision=task["revision"])
