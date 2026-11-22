import json

from fusion.agent_inventory import inventory_payload
from fusion.tests.test_http_server import call, http  # noqa: F401 - fixture


def test_inventory_aggregates_without_secrets(tmp_path):
    data = inventory_payload(tmp_path)
    assert data["agents"]
    assert any(row["id"] == "codex" for row in data["agents"])
    assert all("health" in row and "bound_roles" in row for row in data["agents"])
    encoded = json.dumps(data)
    assert "api_key" not in encoded and "LLM_API_KEY" not in encoded


def test_inventory_is_machine_readable(http):
    httpd, base = http
    assert call(base, "/api/agents/inventory")[0] == 401
    token = "inventory-" + "a" * 40
    httpd.api.add_token("inventory-reader", token)
    status, data = call(base, "/api/agents/inventory", token)
    assert status == 200
    assert data["agents"] and "binding" in data
    assert all("health" in row for row in data["agents"])


def test_agent_runs_endpoint_is_machine_readable(http):
    httpd, base = http
    assert call(base, "/api/agents/codex/runs")[0] == 401
    token = "runs-" + "a" * 40
    httpd.api.add_token("runs-reader", token)
    status, data = call(base, "/api/agents/codex/runs?limit=3", token)
    assert status == 200 and data["agent"] == "codex" and isinstance(data["runs"], list)
