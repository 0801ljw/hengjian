import json
import os

from fusion.cli_models import list_models


def test_codex_cli_models_are_bounded_and_labelled(tmp_path, monkeypatch):
    binary = tmp_path / "codex"
    binary.write_text(
        "#!/bin/sh\n"
        "printf '%s' '{\"models\":[{\"slug\":\"gpt-5\",\"display_name\":\"GPT 5\",\"visibility\":\"list\"},"
        "{\"slug\":\"hidden\",\"display_name\":\"Hidden\",\"visibility\":\"hide\"}]}'\n")
    binary.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    result = list_models("codex")
    assert result["models"] == [{"id": "gpt-5", "label": "GPT 5"}]
