"""fusion approve: turn a board-signed envelope into a completed transition."""
import json
import subprocess
from datetime import datetime, timedelta, timezone

import pytest

from fusion.approve import apply_signed, confirm
from fusion.approvals import approval_document, prepare_request
from fusion.core import PolicyError, Store
from fusion.mutate import payload_hash
from fusion.tests.board_fixture import envelope, keypair


@pytest.fixture
def review_task(tmp_path):
    priv, pub = keypair(tmp_path / "keys")
    store = Store(tmp_path / "db", pub)
    store.create("T", "一页说明")
    for actor, state in (("taizi", "Zhongshu"), ("zhongshu", "Menxia"),
                         ("menxia", "Assigned"), ("shangshu", "Doing")):
        store.move("T", actor, state, "fixture")
    store.move("T", "gongbu", "Review", "fixture", command="done")
    task = store.get("T")
    request = prepare_request(store, "T", "shangshu", "Done", "验收产物",
                              run_id="ai:session1:shangshu",
                              expected_revision=task["revision"],
                              payload_sha256=payload_hash(task["payload"]))
    return store, priv, request


def _sign(priv, request):
    doc = approval_document(request, actor="board:test", run_id="mac-1",
                            expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat())
    return envelope(doc, priv)


def test_signed_request_moves_to_pending_confirm_and_then_done(review_task):
    store, priv, request = review_task
    signed = _sign(priv, request)
    outcome = apply_signed(store, request, signed)
    task = store.get("T")
    assert task["state"] == "PendingConfirm"
    assert task["pending_confirm"]["confirm_by"] == "menxia"
    final = confirm(store, "T", reason="门下复核通过", run_id="menxia:1")
    assert final["state"] == "Done"


def test_binding_fields_come_from_the_request_not_the_caller(review_task):
    """A caller cannot redirect the approved mutation by editing the envelope."""
    store, priv, request = review_task
    signed = _sign(priv, request)
    tampered = json.loads(json.dumps(signed))
    tampered["document"]["scope"] = {"command": "state", "from": "Review", "to": "Cancelled"}
    with pytest.raises(PolicyError):
        apply_signed(store, request, tampered)


def test_stale_request_is_refused(review_task):
    store, priv, request = review_task
    signed = _sign(priv, request)
    store.move("T", "shangshu", "Doing", "返工", command="state")
    with pytest.raises(Exception):
        apply_signed(store, request, signed)


def test_confirm_requires_a_pending_confirmation(review_task):
    store, priv, request = review_task
    with pytest.raises(PolicyError):
        confirm(store, "T", reason="noop", run_id="menxia:2")


def test_cli_confirm_is_a_separate_command(review_task):
    from fusion.approve import confirm_main
    store, priv, request = review_task
    signed = _sign(priv, request)
    apply_signed(store, request, signed)
    assert store.get("T")["state"] == "PendingConfirm"
    assert confirm_main(["--home", str(store.instance_home),
                         "--board-key-file", str(store.board_key_file),
                         "--task", "T", "--run-id", "menxia:cli"]) == 0
    assert store.get("T")["state"] == "Done"
