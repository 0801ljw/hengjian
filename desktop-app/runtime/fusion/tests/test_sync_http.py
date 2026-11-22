import json

from fusion.sync_projection import project_store
from fusion.tests.test_http_server import call, http  # noqa: F401 - pytest fixture


def test_sync_endpoints_are_authenticated_and_cursor_based(http):
    httpd, base = http
    httpd.store.create("SYNC", "sync fixture")
    project_store(httpd.store, httpd.sync_journal)

    assert call(base, "/api/sync/snapshot")[0] == 401
    assert call(base, "/api/sync/events?after=0")[0] == 401

    token = "sync-" + "a" * 40
    httpd.api.add_token("sync-reader", token)
    status, snapshot = call(base, "/api/sync/snapshot", token)
    assert status == 200 and snapshot["cursor"] > 0
    cases = [json.loads(row["payload"]) for row in snapshot["entities"]
             if row["entity_type"] == "case"]
    assert [case["id"] for case in cases] == ["SYNC"]

    status, page = call(base, "/api/sync/events?after=0&limit=10", token)
    assert status == 200 and page["events"]
    assert page["next_cursor"] == page["events"][-1]["seq"]
    assert call(base, "/api/sync/events?after=bad", token)[0] == 400
