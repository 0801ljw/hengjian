"""D3: legacy JSON import is validating, idempotent-safe and rollback-capable."""
import json
import subprocess

import pytest

from fusion.core import Store
from fusion.migrate_legacy import apply, load_legacy, plan


def _store(tmp_path):
    priv, pub = tmp_path / "p.pem", tmp_path / "u.pem"
    subprocess.run(["openssl", "genpkey", "-algorithm", "ed25519", "-out", str(priv)],
                   capture_output=True, check=True)
    subprocess.run(["openssl", "pkey", "-in", str(priv), "-pubout", "-out", str(pub)],
                   capture_output=True, check=True)
    return Store(tmp_path / "db", pub)


def test_valid_records_are_planned_for_import(tmp_path):
    src = tmp_path / "tasks_source.json"
    src.write_text(json.dumps([
        {"id": "T1", "title": "写文档", "state": "Review"},
        {"id": "T2", "title": "做评审", "state": "Doing"},
    ]), encoding="utf-8")
    importable, skipped = plan(load_legacy(src))
    assert [t["id"] for t in importable] == ["T1", "T2"]
    assert skipped == []


def test_terminal_states_are_archived_not_imported(tmp_path):
    src = tmp_path / "tasks_source.json"
    src.write_text(json.dumps([
        {"id": "T1", "title": "已完成", "state": "Done"},
        {"id": "T2", "title": "已取消", "state": "Cancelled"},
    ]), encoding="utf-8")
    importable, skipped = plan(load_legacy(src))
    assert importable == []
    assert {r["id"] for r in skipped} == {"T1", "T2"}
    assert all("archived" in r["reason"] for r in skipped)


def test_bad_records_are_skipped_with_reasons(tmp_path):
    src = tmp_path / "tasks_source.json"
    src.write_text(json.dumps([
        {"id": "ok", "title": "fine", "state": "Review"},
        {"title": "no id", "state": "Done"},
        {"id": "no-title", "state": "Done"},
        {"id": "bad-state", "title": "x", "state": "Nonsense"},
        {"id": "unreachable", "title": "y", "state": "Blocked"},
        {"id": "ok", "title": "dup", "state": "Review"},
    ]), encoding="utf-8")
    importable, skipped = plan(load_legacy(src))
    assert [t["id"] for t in importable] == ["ok"]
    reasons = " ".join(r["reason"] for r in skipped)
    for expected in ("missing id", "missing title", "unknown state", "duplicate id",
                     "terminal state archived"):
        assert expected in reasons


def test_import_replays_through_the_state_machine(tmp_path):
    store = _store(tmp_path)
    apply(store, [{"id": "T1", "title": "写文档", "state": "Review"}])
    task = store.get("T1")
    assert task["state"] == "Review"
    assert len(task["flow_log"]) >= 6


def test_failed_import_restores_the_database(tmp_path):
    store = _store(tmp_path)
    apply(store, [{"id": "T1", "title": "写文档", "state": "Review"}])
    before = store.db.read_bytes()
    bad = [{"id": "T2", "title": "坏的", "state": "Review"},
           {"id": "T1", "title": "重复", "state": "Review"}]  # second raises
    with pytest.raises(Exception):
        apply(store, bad)
    assert store.db.read_bytes() == before
    with pytest.raises(Exception):
        store.get("T2")


def test_missing_or_malformed_source_is_reported(tmp_path):
    from fusion.migrate_legacy import LegacyError
    with pytest.raises(LegacyError):
        load_legacy(tmp_path / "nope.json")
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(LegacyError):
        load_legacy(bad)
