"""Admin-only, fixed-argv user installations. No shell or provider calls."""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import selectors
import signal
import subprocess
import threading
import time
import uuid

from .agent_catalog import catalog
from .agent_env import public as env_public, save as env_save
from .agent_health import health
from .agent_registry import load as load_registry
from .cli_models import list_models
from .live_probe import probe as live_probe
from .agents_probe import find_cli, probe, redact, write_private_json
from .core import PolicyError, now
from .role_binding import apply_preset, load, preset_status, recommendations, save

INSTALL_ARGV = {
    "codex": ("npm", "install", "-g", "@openai/codex"),
    "claude": ("npm", "install", "-g", "@anthropic-ai/claude-code"),
    "pi": ("npm", "install", "-g", "@earendil-works/pi-coding-agent"),
    "aider": ("python3", "-m", "pip", "install", "--user", "--upgrade", "aider-chat"),
    "gemini": ("npm", "install", "-g", "@google/gemini-cli"),
    "opencode": ("npm", "install", "-g", "opencode-ai"),
    "openclaw": ("npm", "install", "-g", "openclaw"),
    "qwen": ("npm", "install", "-g", "@qwen-code/qwen-code"),
    "copilot": ("npm", "install", "-g", "@github/copilot"),
}
UNINSTALL_ARGV = {
    **{name: ("npm", "uninstall", "-g", pkg) for name, pkg in {
        "codex": "@openai/codex", "claude": "@anthropic-ai/claude-code",
        "pi": "@earendil-works/pi-coding-agent", "gemini": "@google/gemini-cli",
        "opencode": "opencode-ai", "openclaw": "openclaw",
        "qwen": "@qwen-code/qwen-code", "copilot": "@github/copilot",
    }.items()},
    "aider": ("python3", "-m", "pip", "uninstall", "-y", "aider-chat"),
}
HERMES_DOCS = "https://hermes-agent.nousresearch.com/docs/getting-started/installation/"
MANUAL = {
    "hermes": {"installable": False, "reason": "需手动安装", "docs": HERMES_DOCS},
    "cursor-agent": {"installable": False, "reason": "需手动安装",
                      "docs": "https://docs.cursor.com/en/cli/overview"},
}
INSTALL_TIMEOUT = 300
TAIL_BYTES = 8192
# Cheap preflight before spawning npm/pip; the installer must say "missing
# node/python" instead of failing 20 seconds into a download.
REQUIRES = {name: ("python3",) if name == "aider" else ("npm",) for name in INSTALL_ARGV}


def preflight(agent):
    import shutil
    missing = [name for name in REQUIRES.get(agent, ()) if shutil.which(name) is None]
    return missing


def installability():
    rows = {}
    for name in INSTALL_ARGV:
        rows[name] = {"installable": True, "requires": list(REQUIRES[name]),
                      "verify": "binary is discoverable after install"}
    rows.update(MANUAL)
    return rows


def _registry_payload(instance):
    agents, source = load_registry(instance)
    return {"agents": agents, "source": source}


class Installer:
    def __init__(self, instance):
        self.instance = Path(instance)
        self.directory = self.instance / "agent-installs"
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)

    def audit(self, who, agent, argv=(), *, started_at=None, exit_code=None,
              duration_sec=0, status="rejected"):
        # Never persist arbitrary input, credentials, environment or exception text.
        record = {"who": who, "agent": agent if agent in installability() else "invalid",
                  "argv": list(argv), "started_at": started_at or now(),
                  "exit_code": exit_code, "duration_sec": duration_sec, "status": status}
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW
        with os.fdopen(os.open(self.directory / "audit.jsonl", flags, 0o600), "a") as out:
            fcntl.flock(out, fcntl.LOCK_EX)
            out.write(json.dumps(redact(record), ensure_ascii=False) + "\n")
            out.flush()
            os.fsync(out.fileno())

    def handle(self, method, path, body, session):
        agent = None
        if method == "POST" and path == "/api/agents/install":
            try:
                data = json.loads(body)
                if isinstance(data, dict) and isinstance(data.get("agent"), str):
                    agent = data["agent"]
            except (ValueError, UnicodeError):
                data = None
        else:
            data = None
        who = session["name"] if session else "anonymous"
        if not session or session["role"] != "admin":
            if path == "/api/agents/install" and method == "POST":
                self.audit(who, agent)
            return (403 if session else 401), {"error": "admin session required"}
        if path == "/api/agents/env" and method == "GET":
            return 200, {"agents": env_public(self.instance)}
        if path == "/api/agents/env" and method == "POST":
            try:
                data = json.loads(body or b"{}")
                if not isinstance(data, dict) or not isinstance(data.get("agent"), str):
                    raise PolicyError("agent is required")
                return 200, {"saved": env_save(self.instance, data["agent"], data.get("values") or {})}
            except (PolicyError, ValueError) as exc:
                return 400, {"error": str(exc)}
        if path == "/api/agents/live-probe" and method == "POST":
            try:
                data = json.loads(body or b"{}")
                if not isinstance(data, dict) or not isinstance(data.get("agent"), str):
                    raise PolicyError("agent is required")
                return 200, live_probe(data["agent"], data.get("model"), self.instance)
            except (PolicyError, ValueError) as exc:
                return 400, {"error": str(exc)}
        if path == "/api/agents/models" and method == "POST":
            try:
                data = json.loads(body or b"{}")
                agent = data.get("agent") if isinstance(data, dict) else None
                return 200, list_models(agent)
            except (PolicyError, ValueError) as exc:
                return 400, {"error": str(exc)}
        if path == "/api/agents" and method == "GET":
            facts = probe(self.instance)
            return 200, {"agents": facts, "preset": preset_status(facts),
                         "installability": installability(), "binding": load(self.instance),
                         "recommendations": recommendations(),
                         "catalog": catalog(facts, installability(), self.instance),
                         "health": {name: health(name, facts, self.instance) for name in facts},
                         "registry": _registry_payload(self.instance)}
        if path == "/api/agents/bind" and method == "POST":
            from .core import PolicyError
            try:
                data = json.loads(body or b"{}")
                if not isinstance(data, dict):
                    raise ValueError("payload must be an object")
                if data:
                    # Custom per-role binding: {mode, bindings:{role:{agent,model}}}
                    return 200, save(self.instance, data)
                return 200, apply_preset(probe(self.instance, refresh=True), self.instance)
            except (PolicyError, ValueError, KeyError) as exc:
                return 400, {"error": str(exc)}
        if path.startswith("/api/agents/install/") and method == "GET":
            job = path.removeprefix("/api/agents/install/")
            if len(job) != 32 or any(c not in "0123456789abcdef" for c in job):
                return 404, {"error": "unknown job"}
            try:
                return 200, json.loads((self.directory / (job + ".json")).read_text())
            except FileNotFoundError:
                return 404, {"error": "unknown job"}
        if path not in ("/api/agents/install", "/api/agents/update", "/api/agents/uninstall") or method != "POST":
            return 405, {"error": "method not allowed"}
        action = path.rsplit("/", 1)[-1]
        if not isinstance(data, dict) or set(data) != {"agent"} or agent not in INSTALL_ARGV:
            self.audit(who, agent)
            manual = MANUAL.get(agent, {})
            return 400, {"error": manual.get("reason", "unknown agent or invalid payload"),
                         "docs": manual.get("docs")}
        return self._start(agent, action, who)

    def _start(self, agent, action, who):
        argv = UNINSTALL_ARGV[agent] if action == "uninstall" else INSTALL_ARGV[agent]
        missing = preflight(agent)
        if missing:
            self.audit(who, agent)
            return 400, {"error": "缺少安装前置依赖：" + "、".join(missing),
                         "requires": missing}
        lock = (self.directory / (agent + ".lock")).open("a")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock.close()
            self.audit(who, agent)
            return 409, {"error": "installation already running"}
        try:
            found = bool(probe(self.instance, refresh=True).get(agent, {}).get("found"))
            if action == "install" and found:
                self.audit(who, agent)
                lock.close()
                return 409, {"error": "agent already installed"}
            if action in ("update", "uninstall") and not found:
                self.audit(who, agent)
                lock.close()
                return 400, {"error": "agent is not installed"}
            job = uuid.uuid4().hex
            started_at = now()
            state = {"job": job, "status": "running", "tail": "", "exit_code": None,
                     "action": action}
            target = self.directory / (job + ".json")
            write_private_json(target, state)
            audit_target = self.directory / (job + ".audit.json")
            write_private_json(audit_target, {"who": who, "agent": agent,
                               "argv": list(argv), "started_at": started_at,
                               "exit_code": None, "duration_sec": 0, "action": action})
            threading.Thread(target=self._run,
                             args=(target, audit_target, state, who, agent, started_at, lock,
                                   argv, action), daemon=True).start()
            return 202, state
        except Exception:
            lock.close()
            raise

    def _run(self, target, audit_target, state, who, agent, started_at, lock, argv, action):
        start = time.monotonic()
        tail = b""
        process = None
        # A minimal environment prevents inherited model/API/npm credentials.
        environment = {key: os.environ[key] for key in ("PATH", "HOME", "LANG", "TMPDIR")
                       if key in os.environ}
        environment.update(npm_config_prefix=str(Path.home() / ".local"),
                           npm_config_userconfig=os.devnull, npm_config_globalconfig=os.devnull)
        try:
            process = subprocess.Popen(argv, stdin=subprocess.DEVNULL,
                                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                       env=environment, start_new_session=True)
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while True:
                    remaining = INSTALL_TIMEOUT - (time.monotonic() - start)
                    if remaining <= 0:
                        state["status"] = "timeout"
                        break
                    events = selector.select(min(remaining, 0.2))
                    if events:
                        chunk = os.read(process.stdout.fileno(), 4096)
                        if not chunk:
                            process.wait(timeout=max(0.001, remaining))
                            state["status"] = "succeeded" if process.returncode == 0 else "failed"
                            break
                        tail = (tail + chunk)[-TAIL_BYTES:]
                        cleaned = "\n".join(redact(line) for line in tail.decode("utf-8", "replace").splitlines())
                        state["tail"] = cleaned.encode("utf-8")[-TAIL_BYTES:].decode("utf-8", "ignore")
                        write_private_json(target, state)
        except subprocess.TimeoutExpired:
            state["status"] = "timeout"
        except Exception:
            state.update(status="failed", tail="安装失败，请检查本机 npm 和用户目录权限。")
        finally:
            if process is not None:
                # Kill the whole group, including descendants after wrapper exit.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
                process.stdout.close()
                state["exit_code"] = process.returncode
            try:
                duration = round(time.monotonic() - start, 3)
                record = {"who": who, "agent": agent, "argv": list(argv),
                          "action": action, "started_at": started_at, "exit_code": state["exit_code"],
                          "duration_sec": duration, "status": state["status"]}
                write_private_json(audit_target, redact(record))
                self.audit(who, agent, argv, started_at=started_at,
                           exit_code=state["exit_code"], duration_sec=duration, status=state["status"])
                if state["status"] == "succeeded":
                    probe(self.instance, refresh=True)
                    found = bool(find_cli(agent))
                    if action == "uninstall" and found:
                        state.update(status="failed",
                                     tail=(state.get("tail", "") + "\n卸载命令完成，但仍能找到可执行文件。").strip())
                    elif action != "uninstall" and not found:
                        state.update(status="failed",
                                     tail=(state.get("tail", "") + "\n安装命令完成，但没有找到可执行文件。").strip())
            finally:
                write_private_json(target, state)
                lock.close()
