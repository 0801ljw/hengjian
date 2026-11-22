"""Bundled registry remains the local behavior source."""
import json

from fusion.agent_registry import load


def test_bundled_registry_is_available_without_network(monkeypatch):
    monkeypatch.delenv("DUTANG_AGENT_REGISTRY_URL", raising=False)
    rows, source = load(None)
    assert source == "bundled"
    assert {"codex", "gemini", "opencode", "openclaw", "qwen", "copilot"} <= {r["id"] for r in rows}


def test_fresh_cache_is_used_without_network(tmp_path, monkeypatch):
    monkeypatch.delenv("DUTANG_AGENT_REGISTRY_URL", raising=False)
    cache = tmp_path / "agent-registry-cache.json"
    cache.write_text(json.dumps([{"id": "codex", "title": "cached"}]))
    rows, source = load(tmp_path)
    assert source == "cache"
    assert rows[0]["title"] == "cached"
    assert rows[0]["integrated"] is True  # bundled behavior field wins
