import shutil

from fusion.agent_install import preflight


def test_preflight_names_missing_runtime(monkeypatch):
    real = shutil.which
    monkeypatch.setattr(shutil, "which", lambda name: None if name == "npm" else real(name))
    missing = preflight("gemini")
    assert "npm" in missing
