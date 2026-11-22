"""P5-4: the runtime child must not inherit secrets."""
import os
from pathlib import Path

import pytest

from fusion.runtime import child_env


@pytest.fixture
def workspace(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    return ws


def test_secrets_are_not_passed_to_the_child(workspace, monkeypatch):
    monkeypatch.setenv("PENGUIN_GPT_API_KEY", "sk-should-not-leak")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws-should-not-leak")
    monkeypatch.setenv("GITHUB_TOKEN", "gh-should-not-leak")
    monkeypatch.setenv("SOME_SERVICE_TOKEN", "tok-should-not-leak")
    monkeypatch.setenv("PAPERCLIP_API_KEY", "pc-should-not-leak")
    monkeypatch.setenv("CODEX_HOME", "/secret/codex")
    env = child_env(workspace, allow="")
    blob = "\n".join(f"{k}={v}" for k, v in env.items())
    for leaked in ("sk-should-not-leak", "aws-should-not-leak", "gh-should-not-leak",
                   "tok-should-not-leak", "pc-should-not-leak", "/secret/codex"):
        assert leaked not in blob
    for name in ("PENGUIN_GPT_API_KEY", "AWS_SECRET_ACCESS_KEY", "GITHUB_TOKEN",
                 "SOME_SERVICE_TOKEN", "PAPERCLIP_API_KEY", "CODEX_HOME"):
        assert name not in env


def test_harmless_variables_pass_through(workspace, monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("LANG", "zh_CN.UTF-8")
    monkeypatch.setenv("LC_ALL", "zh_CN.UTF-8")
    monkeypatch.setenv("TERM", "xterm")
    env = child_env(workspace, allow="")
    assert env["PATH"] == "/usr/bin:/bin"
    assert env["LANG"] == "zh_CN.UTF-8"
    assert env["LC_ALL"] == "zh_CN.UTF-8"
    assert env["TERM"] == "xterm"


def test_explicit_allowlist_admits_named_variable(workspace, monkeypatch):
    monkeypatch.setenv("FUSION_TEST_VAR", "ok-value")
    assert "FUSION_TEST_VAR" not in child_env(workspace, allow="")
    assert child_env(workspace, allow="FUSION_TEST_VAR")["FUSION_TEST_VAR"] == "ok-value"


def test_allowlist_cannot_smuggle_secrets(workspace, monkeypatch):
    monkeypatch.setenv("PENGUIN_GPT_API_KEY", "sk-nope")
    assert "PENGUIN_GPT_API_KEY" not in child_env(workspace, allow="PENGUIN_GPT_API_KEY")
    monkeypatch.setenv("MY_THING_SECRET", "s3cr3t")
    assert "MY_THING_SECRET" not in child_env(workspace, allow="MY_THING_SECRET")


def test_child_home_is_inside_workspace(workspace):
    env = child_env(workspace, allow="")
    assert Path(env["HOME"]).resolve().is_relative_to(workspace.resolve())
    assert Path(env["HOME"]).is_dir()
    assert Path(env["TMPDIR"]).is_dir()
