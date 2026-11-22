import sqlite3

import pytest

from fusion.sync_journal import GENESIS, SyncError, SyncJournal


@pytest.fixture
def journal(tmp_path):
    return SyncJournal(tmp_path / "sync.sqlite3")


def append(journal, *, version=1, mutation_id="m1", op="upsert", entity_id="AI-1",
           payload=None, workspace_id="w_1"):
    return journal.append(
        workspace_id=workspace_id,
        device_id="d_1",
        entity_type="case",
        entity_id=entity_id,
        version=version,
        mutation_id=mutation_id,
        op=op,
        payload=payload if payload is not None else {"state": "Taizi"},
    )


def test_append_after_snapshot_and_chain(journal):
    first = append(journal, version=1, mutation_id="m1", payload={"state": "Taizi"})
    second = append(journal, version=2, mutation_id="m2", payload={"state": "Zhongshu"})
    assert first["seq"] == 1 and second["seq"] == 2
    assert first["prev_hash"] == GENESIS
    assert second["prev_hash"] == first["hash"]
    assert journal.verify_chain("w_1")

    page = journal.after("w_1", 0)
    assert [row["mutation_id"] for row in page["events"]] == ["m1", "m2"]
    assert page["next_cursor"] == 2 and page["has_more"] is False

    page = journal.after("w_1", 1)
    assert [row["mutation_id"] for row in page["events"]] == ["m2"]
    assert page["next_cursor"] == 2

    snapshot = journal.snapshot("w_1")
    assert snapshot["cursor"] == 2
    assert len(snapshot["entities"]) == 1
    assert snapshot["entities"][0]["payload"] == '{"state":"Zhongshu"}'


def test_duplicate_mutation_is_idempotent(journal):
    first = append(journal, version=1, mutation_id="same")
    second = append(journal, version=1, mutation_id="same")
    assert second == first
    assert len(journal.after("w_1", 0)["events"]) == 1


def test_version_must_increase_and_tombstone_is_final(journal):
    append(journal, version=1, mutation_id="m1")
    with pytest.raises(SyncError):
        append(journal, version=1, mutation_id="m2")
    append(journal, version=2, mutation_id="m3", op="tombstone")
    with pytest.raises(SyncError):
        append(journal, version=3, mutation_id="m4", op="upsert")


def test_cursor_paging(journal):
    for version in (1, 2, 3):
        append(journal, version=version, mutation_id=f"m{version}")
    page = journal.after("w_1", 0, limit=2)
    assert page["has_more"] is True
    assert page["next_cursor"] == 2
    page = journal.after("w_1", page["next_cursor"], limit=2)
    assert page["has_more"] is False
    assert [row["version"] for row in page["events"]] == [3]


def test_tamper_breaks_chain(journal):
    append(journal, version=1, mutation_id="m1")
    append(journal, version=2, mutation_id="m2")
    with sqlite3.connect(journal.path) as conn:
        conn.execute("UPDATE sync_events SET payload=? WHERE seq=1", ('{"state":"tampered"}',))
    with pytest.raises(SyncError):
        journal.verify_chain("w_1")


def test_unknown_workspace_is_empty(journal):
    assert journal.after("w_missing", 0) == {
        "events": [], "next_cursor": 0, "has_more": False}
    assert journal.snapshot("w_missing") == {"entities": [], "cursor": 0}
    assert journal.verify_chain("w_missing") is True
