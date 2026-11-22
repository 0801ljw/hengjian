import json
import sqlite3

from fusion.core import Store
from fusion.sync_journal import SyncJournal
from fusion.sync_projection import project_store


def _store(tmp_path):
    store = Store(tmp_path / "instance")
    store.create("T", "projection fixture")
    for actor, target in (("taizi", "Zhongshu"), ("zhongshu", "Menxia"),
                          ("menxia", "Assigned"), ("shangshu", "Doing")):
        store.move("T", actor, target, "fixture")
    return store


def _case(journal):
    rows = [row for row in journal.snapshot("w_local")["entities"]
            if row["entity_type"] == "case"]
    assert len(rows) == 1
    return json.loads(rows[0]["payload"])


def test_projection_is_read_only_idempotent_and_tracks_revision(tmp_path):
    store = _store(tmp_path)
    before = store.snapshot()
    journal = SyncJournal(tmp_path / "journal.sqlite3")

    with sqlite3.connect(store.db) as conn:
        expected_events = conn.execute("SELECT count(*) FROM events").fetchone()[0]
    first = project_store(store, journal)
    assert first["events_projected"] == expected_events
    assert first["cases_projected"] == 1
    assert _case(journal)["state"] == "Doing"
    assert journal.verify_chain("w_local")
    assert store.snapshot() == before

    second = project_store(store, journal)
    assert second["cursor"] == first["cursor"]
    assert second["events_projected"] == first["events_projected"]
    assert len(journal.after("w_local", 0)["events"]) == expected_events + 1

    store.move("T", "gongbu", "Review", "fixture review", command="done")
    with sqlite3.connect(store.db) as conn:
        expected_after = conn.execute("SELECT count(*) FROM events").fetchone()[0]
    third = project_store(store, journal)
    assert third["cursor"] > second["cursor"]
    assert third["events_projected"] == expected_after
    assert _case(journal)["state"] == "Review"
    assert journal.verify_chain("w_local")
    assert len(journal.after("w_local", 0)["events"]) == expected_after + 2
