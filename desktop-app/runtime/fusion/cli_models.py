"""Bounded CLI model discovery for subscription-based agents.

Only commands shipped by the agent CLIs are used; no shell and no prompt is
sent. A CLI that cannot answer returns an explicit error instead of a guessed
catalog.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import time

from .agents_probe import find_cli
from .core import PolicyError

MAX = 100
TIMEOUT = 20


def _parse_codex(text):
    try:
        data = json.loads(text)
    except ValueError:
        return []
    rows = []
    seen = set()
    entries = data.get("models") if isinstance(data, dict) else None
    for entry in entries or []:
        if not isinstance(entry, dict) or entry.get("visibility") != "list":
            continue
        identifier = str(entry.get("slug") or "").strip()
        if not identifier or identifier in seen:
            continue
        seen.add(identifier)
        label = str(entry.get("display_name") or identifier).strip()[:80]
        rows.append({"id": identifier[:128], "label": label or identifier})
    return rows[:MAX]


def _parse_pi(text):
    rows = []
    seen = set()
    in_table = False
    for raw in text.splitlines():
        fields = raw.split()
        if fields == ["provider", "model", "context", "max-out", "thinking", "images"]:
            in_table = True
            continue
        if not in_table or len(fields) != 6:
            continue
        provider, model = fields[0], fields[1]
        if not all(ch.isalnum() or ch in "._-" for ch in provider):
            continue
        if not all(ch.isalnum() or ch in ".:@/[]_-" for ch in model):
            continue
        identifier = f"{provider}/{model}"
        if identifier in seen:
            continue
        seen.add(identifier)
        rows.append({"id": identifier[:128], "label": f"{model} ({provider})"[:80]})
        if len(rows) >= MAX:
            break
    return rows


COMMANDS = {
    "codex": (["codex", "debug", "models"], _parse_codex, 30),
    "pi": (["pi", "--no-approve", "--no-extensions", "--no-skills", "--list-models"], _parse_pi, 30),
}


def list_models(agent):
    if agent not in COMMANDS:
        raise PolicyError("该 Agent 不支持 CLI 模型清单")
    argv, parser, timeout = COMMANDS[agent]
    binary = find_cli(argv[0])
    if not binary:
        raise PolicyError(f"未找到 {argv[0]} CLI")
    argv = [binary, *argv[1:]]
    env = {key: os.environ[key] for key in ("PATH", "HOME", "LANG", "TMPDIR") if key in os.environ}
    env.update({"NO_COLOR": "1", "TERM": "dumb", "CI": "1"})
    with tempfile.TemporaryDirectory(prefix="dutang-models-") as directory:
        process = subprocess.Popen(argv, cwd=directory, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   env=env, start_new_session=True)
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
            raise PolicyError("CLI 模型清单超时") from None
    if process.returncode:
        message = (stderr or stdout)[-500:].decode("utf-8", "replace").strip()
        raise PolicyError(f"CLI 模型清单失败：{message[:160]}")
    rows = parser(stdout.decode("utf-8", "replace"))
    if not rows:
        raise PolicyError("CLI 没有返回可识别的模型列表")
    return {"agent": agent, "models": rows, "source": "cli"}
