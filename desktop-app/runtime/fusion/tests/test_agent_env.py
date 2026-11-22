import os

from fusion.agent_env import load, public, resolve, save


def test_save_mask_and_native_mapping(tmp_path, monkeypatch):
    monkeypatch.setattr("fusion.agent_env.Path.home", lambda: tmp_path)
    saved = save(tmp_path, "gemini", {"LLM_API_KEY": "fixture-key",
                                      "LLM_BASE_URL": "https://relay.example/v1",
                                      "LLM_MODEL": "gemini-test"})
    assert saved["LLM_API_KEY"] == "***"
    assert public(tmp_path)["gemini"]["LLM_API_KEY"] == "***"
    data = resolve("gemini", tmp_path)
    assert data["key"] == "fixture-key"
    assert data["env"]["GEMINI_API_KEY"] == "fixture-key"
    assert data["env"]["GOOGLE_GEMINI_BASE_URL"].startswith("https://")
    # Omitting the key preserves it.
    save(tmp_path, "gemini", {"LLM_MODEL": "next"})
    assert load(tmp_path)["gemini"]["LLM_API_KEY"] == "fixture-key"
    assert (tmp_path / "agent-env.json").stat().st_mode & 0o777 == 0o600
