"""Real CLI runtime. No mock fallback, provider change or shell interpolation."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class RunRequest:
    run_id: str
    task_id: str
    agent_id: str
    workspace: Path
    prompt: str
    timeout_sec: int = 180


@dataclass
class RunResult:
    status: str
    exit_code: int | None
    usage: dict | None
    elapsed_sec: float
    stdout: str
    stderr: str
    cost_usd: float | None = None

    def to_dict(self) -> dict:
        return asdict(self)


# ── 进程隔离：子进程环境白名单 ─────────────────────────────────────────────
# 默认拒绝。只透传与执行有关、且不含秘密的变量；需要额外变量时用
# FUSION_RUNTIME_ENV_ALLOW="NAME1,NAME2" 显式放行（由可信运维设置）。
_ENV_ALLOW = {
    "PATH", "LANG", "LANGUAGE", "TERM", "TMPDIR", "TZ", "SHELL",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE",
}
_ENV_ALLOW_PREFIX = ("LC_",)
# 即使被显式放行也永不透传（防止把密钥误配进 allowlist）
_ENV_DENY_PREFIX = ("PAPERCLIP_", "CODEX_", "AWS_", "SSH_", "PENGUIN_", "GITHUB_",
                    "AZURE_", "GOOGLE_", "OPENAI_", "ANTHROPIC_")
_ENV_DENY_SUFFIX = ("_API_KEY", "_TOKEN", "_SECRET", "_PASSWORD", "_CREDENTIALS")


def child_env(workspace: Path, *, allow: str | None = None) -> dict:
    """Build a denylisted-by-default environment for the runtime child."""
    explicit = {n.strip() for n in (allow if allow is not None
                                    else os.environ.get("FUSION_RUNTIME_ENV_ALLOW", "")).split(",")
                if n.strip()}
    env = {}
    for name, value in os.environ.items():
        if name.startswith(_ENV_DENY_PREFIX) or name.endswith(_ENV_DENY_SUFFIX):
            continue
        if name in _ENV_ALLOW or name.startswith(_ENV_ALLOW_PREFIX) or name in explicit:
            env[name] = value
    home = workspace / "home"
    home.mkdir(parents=True, exist_ok=True)
    env["HOME"] = str(home)
    env["TMPDIR"] = str(home / "tmp")
    Path(env["TMPDIR"]).mkdir(parents=True, exist_ok=True)
    return env


class RuntimeAdapter(Protocol):
    capabilities: dict

    def execute(self, request: RunRequest) -> RunResult: ...


class CodexLocal:
    capabilities = {"filesystem": True, "commands": True,
                    "usage_events": True, "isolated": False,
                    "env_allowlisted": True}

    def execute(self, request: RunRequest) -> RunResult:
        workspace = request.workspace.resolve(strict=True)
        stdout = workspace / "runtime.jsonl"
        stderr = workspace / "runtime.stderr.log"
        env = child_env(workspace)
        command = ["codex", "exec", "--json", "--ephemeral", "--color", "never",
                   "--skip-git-repo-check", "--sandbox", "workspace-write",
                   "-C", str(workspace), "-"]
        started = time.monotonic()
        code = None
        status = "unavailable"
        with stdout.open("w") as out, stderr.open("w") as err:
            try:
                proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=out,
                                        stderr=err, text=True, env=env,
                                        cwd=workspace, start_new_session=True)
                try:
                    proc.communicate(request.prompt, timeout=request.timeout_sec)
                    code = proc.returncode
                    status = "succeeded" if code == 0 else "failed"
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait()
                    code = proc.returncode
                    status = "timeout"
            except OSError as exc:
                err.write(f"{type(exc).__name__}: {exc}\n")
        usage = None
        for line in stdout.read_text().splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "turn.completed":
                usage = event.get("usage")
        return RunResult(status, code, usage, round(time.monotonic() - started, 3),
                         str(stdout), str(stderr))
