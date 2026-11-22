"""P2-0 offline cryptographic boundary; all private keys are disposable fixtures."""
import copy
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest

from fusion.approvals import approval_document, import_approval, prepare_request
from fusion.core import ROOT, PolicyError, Store
from fusion.mutate import canonical, payload_hash
from fusion.tests.board_fixture import envelope, keypair


@pytest.fixture
def approval(tmp_path):
    private, public = keypair(tmp_path / "keys")
    store = Store(tmp_path / "db", public)
    store.create("T", "离线测试")
    for actor, state in (("taizi", "Zhongshu"), ("zhongshu", "Menxia"),
                         ("menxia", "Assigned"), ("shangshu", "Doing")):
        store.move("T", actor, state, "fixture")
    store.move("T", "gongbu", "Review", "fixture", command="done")
    task = store.get("T")
    request = prepare_request(store, "T", "shangshu", "Done", "review done", run_id="request",
                              expected_revision=task["revision"], payload_sha256=payload_hash(task["payload"]))
    document = approval_document(request, actor="board:test", run_id="human-test",
                                 expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat())
    return store, private, request, envelope(document, private)


def test_valid_signature_public_key_only_and_canonical_order(approval):
    store, private, _, signed = approval
    # Verification still works after the only fixture signing key is removed.
    private.unlink()
    signed["document"] = dict(reversed(list(signed["document"].items())))
    assert import_approval(store, signed) == signed["document"]


@pytest.mark.parametrize("field", ["id", "kind", "task_id", "request_id", "request_actor",
                                    "request_run_id", "payload_sha256", "revision", "scope", "actor",
                                    "run_id", "created_at", "expires_at", "decision", "principal"])
def test_every_document_field_is_signed(approval, field):
    store, _, _, signed = approval
    before = store.snapshot()
    signed["document"][field] = {"tampered": True}
    with pytest.raises(PolicyError, match="signature"):
        import_approval(store, signed)
    assert store.snapshot() == before


def test_wrong_public_key_rejected_without_writes(approval, tmp_path):
    store, _, _, signed = approval
    _, wrong = keypair(tmp_path / "wrong")
    store.board_key_file = wrong
    before = store.snapshot()
    with pytest.raises(PolicyError, match="signature"):
        import_approval(store, signed)
    assert store.snapshot() == before


@pytest.mark.parametrize("change", ["missing-alg", "hmac", "unknown", "missing-document", "extra",
                                     "short", "nonhex", "not-string", "array-document", "nan"])
def test_bad_envelope_fails_closed(approval, change):
    store, _, _, signed = approval
    if change == "missing-alg":
        signed.pop("alg")
    elif change in {"hmac", "unknown"}:
        signed["alg"] = change
    elif change == "missing-document":
        signed.pop("document")
    elif change == "extra":
        signed["key_file"] = "/untrusted/key"
    elif change == "array-document":
        signed["document"] = []
    elif change == "nan":
        signed["document"]["revision"] = float("nan")
    else:
        signed["signature"] = {"short": "a" * 64, "nonhex": "x" * 128, "not-string": 12}[change]
    with pytest.raises(PolicyError):
        import_approval(store, signed)


@pytest.mark.parametrize("key", ["private", "missing", "garbage", "rsa", "legacy-hmac"])
def test_verifier_rejects_non_ed25519_public_files(approval, tmp_path, key):
    store, private, _, signed = approval
    path = tmp_path / "invalid-key"
    if key == "private":
        path = private
    elif key in {"garbage", "legacy-hmac"}:
        path.write_bytes(b"bad PEM" if key == "garbage" else b"old symmetric fixture key" * 3)
    elif key == "rsa":
        rsa = tmp_path / "TEST-ONLY-rsa.pem"
        subprocess.run(["openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048",
                        "-out", str(rsa)], capture_output=True, check=True, timeout=10)
        subprocess.run(["openssl", "pkey", "-in", str(rsa), "-pubout", "-out", str(path)],
                       capture_output=True, check=True, timeout=10)
    store.board_key_file = path
    with pytest.raises(PolicyError):
        import_approval(store, signed)


@pytest.mark.parametrize("failure", [FileNotFoundError(), subprocess.TimeoutExpired("openssl", 10)])
def test_openssl_unavailable_or_timeout_fails_closed(approval, monkeypatch, failure):
    store, _, _, signed = approval
    def fail(*args, **kwargs):
        raise failure
    monkeypatch.setattr("fusion.approvals.subprocess.run", fail)
    with pytest.raises(PolicyError):
        import_approval(store, signed)


@pytest.mark.parametrize("case", ["expired", "self-run", "stale-payload", "nested-scope"])
def test_signed_invalid_decision_still_rejected(approval, case):
    store, private, _, signed = approval
    document = copy.deepcopy(signed["document"])
    if case == "expired":
        document.update(created_at="2020-01-01T00:00:00+00:00", expires_at="2020-01-02T00:00:00+00:00")
    elif case == "self-run":
        document["run_id"] = document["request_run_id"]
    elif case == "stale-payload":
        store.move("T", "shangshu", "Review", "changed", command="payload", payload={"amount": 999})
    else:
        document["scope"]["to"] = "Cancelled"
    with pytest.raises(PolicyError):
        import_approval(store, envelope(document, private))


@pytest.mark.parametrize("tool", ["standalone", "board"])
def test_manual_tools_test_keys_round_trip(approval, tmp_path, tool):
    store, private, request, signed = approval
    path = tmp_path / "input.json"
    # Isolated subprocess simulates an offline human terminal using TEST keys.
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PAPERCLIP_", "CODEX_"))}
    if tool == "standalone":
        path.write_text(json.dumps(signed["document"], ensure_ascii=False, indent=4), encoding="utf-8")
        # Copy the one file outside the repo: no fusion package or third-party imports.
        script = tmp_path / "board-sign.py"
        script.write_bytes((ROOT / "fusion/board-sign.py").read_bytes())
        command = [sys.executable, str(script), "--document", str(path), "--private-key", str(private)]
    else:
        path.write_text(json.dumps(request), encoding="utf-8")
        command = [sys.executable, "-m", "fusion.board", "--request", str(path),
                   "--private-key", str(private), "--actor", "board:test", "--run-id", "human-test",
                   "--expires-at", signed["document"]["expires_at"]]
    result = subprocess.run(command, cwd=ROOT if tool == "board" else tmp_path,
                            env=env, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    output = json.loads(result.stdout)
    assert output["alg"] == "ed25519"
    assert import_approval(store, output) == output["document"]
    if tool == "standalone":
        # Ed25519 is deterministic; proves byte-for-byte canonical JSON parity.
        assert output["signature"] == signed["signature"]
        assert canonical(output["document"]) == canonical(signed["document"])


def test_standalone_signer_refuses_agent_environment_before_reading_key(tmp_path):
    result = subprocess.run([sys.executable, str(ROOT / "fusion/board-sign.py"),
                             "--document", "missing", "--private-key", "missing"],
                            env=dict(os.environ, PAPERCLIP_RUN_ID="test"),
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 2
    assert "independent human terminal" in result.stderr


def test_stored_signature_is_rechecked_on_consumption(approval):
    store, _, _, signed = approval
    import_approval(store, signed)
    with store.transaction() as conn:
        signed["document"]["actor"] = "board:tampered"
        conn.execute("UPDATE decisions SET body=? WHERE id=?",
                     (canonical(signed), signed["document"]["id"]))
    with pytest.raises(PolicyError, match="signature"):
        store.move("T", "shangshu", "Done", "review done", run_id="request",
                   approval_id=signed["document"]["id"])


def test_controller_defaults_to_host_public_key(tmp_path, monkeypatch, capsys):
    import fusion.control as control
    original = control.Store
    configured = []
    def capture(home, key):
        configured.append(str(key))
        return original(home, key)
    monkeypatch.setattr(control, "Store", capture)
    monkeypatch.setattr(sys, "argv", ["control", "--home", str(tmp_path), "snapshot"])
    assert control.main() == 0
    assert configured == ["/home/ljw/.config/fusion/board_ed25519.pub.pem"]
    assert json.loads(capsys.readouterr().out)["tasks"] == []
