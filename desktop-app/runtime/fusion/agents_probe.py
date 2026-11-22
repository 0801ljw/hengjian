"""Bounded, local-only discovery. Facts here are not capability assessments."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import time
import tomllib

CANDIDATES = ("codex", "hermes", "pi", "claude", "gemini", "aider", "cursor-agent",
              "opencode", "openclaw", "qwen", "copilot")
CONFIG_DIRS = dict(zip(CANDIDATES, (".codex", ".hermes", ".pi", ".claude", ".gemini",
                                         ".aider", ".cursor", ".config/opencode",
                                         ".openclaw", ".qwen", ".copilot")))
ENV_KEYS = {
    "codex": ("OPENAI_API_KEY",), "hermes": ("OPENROUTER_API_KEY", "OPENAI_API_KEY"),
    "pi": ("ANTHROPIC_API_KEY", "OPENAI_API_KEY"), "claude": ("ANTHROPIC_API_KEY",),
    "gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY"), "aider": ("OPENAI_API_KEY",),
    "cursor-agent": ("CURSOR_API_KEY",), "opencode": ("ANTHROPIC_API_KEY", "OPENAI_API_KEY"),
    "openclaw": ("OPENAI_API_KEY", "ANTHROPIC_API_KEY"),
    "qwen": ("DASHSCOPE_API_KEY", "OPENAI_API_KEY"),
    "copilot": ("GITHUB_TOKEN", "GH_TOKEN"),
}
VERSION_TIMEOUT = 5
CACHE_TTL = 24 * 60 * 60


def write_private_json(path, value):
    """Atomic replacement; never expose a partly written or world-readable file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix="." + path.name)
    try:
        with os.fdopen(fd, "w") as output:
            json.dump(value, output, ensure_ascii=False, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _secret_names(extra=()):
    return {name for name in os.environ
            if any(word in name.upper() for word in ("KEY", "TOKEN", "SECRET", "PASSWORD"))} | set(extra)


def redact(value, extra=()):
    """Defense in depth for even a broken --version printing credentials."""
    secrets = sorted({os.environ[name] for name in _secret_names(extra)
                      if os.environ.get(name)}, key=len, reverse=True)

    def clean(item):
        if isinstance(item, str):
            for secret in secrets:
                item = item.replace(secret, "[REDACTED]")
            # Prevent terminal control sequences from version/config data.
            return "".join(char if char.isprintable() else " " for char in item)
        if isinstance(item, list):
            return [clean(entry) for entry in item]
        if isinstance(item, dict):
            return {clean(key): clean(entry) for key, entry in item.items()}
        return item
    return clean(value)


def find_cli(name):
    """PATH first, then npm/pip user prefixes used by the one-click installer."""
    found = shutil.which(name)
    if found:
        return found
    home = Path.home()
    for directory in (home / ".local" / "bin", home / ".npm-global" / "bin",
                      home / "bin", home / ".local" / "share" / "pnpm"):
        candidate = directory / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def _version(path, secret_names):
    # A file instead of PIPE prevents descendants holding stdout open forever.
    # Only version output is collected, never model output or credential files.
    environment = {k: v for k, v in os.environ.items() if k not in secret_names}
    try:
        with tempfile.TemporaryFile() as output:
            with subprocess.Popen([path, "--version"], stdout=output, stderr=subprocess.DEVNULL,
                                  stdin=subprocess.DEVNULL, env=environment,
                                  start_new_session=True) as process:
                try:
                    process.wait(timeout=VERSION_TIMEOUT)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=VERSION_TIMEOUT)
                    return "timeout"
                finally:
                    # Also reap descendants of a wrapper that exited early.
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                if process.returncode:
                    return "error"
                output.seek(0)
                return redact(output.read(4096).decode("utf-8", errors="replace").strip(),
                              secret_names) or "unknown"
    except OSError:
        return "error"


def _config_facts():
    home = Path.home()
    codex = Path(os.environ.get("CODEX_HOME", str(home / ".codex")))
    try:
        data = tomllib.loads((codex / "config.toml").read_text())
        status = "read"
    except FileNotFoundError:
        data, status = {}, "missing"
    except (OSError, ValueError):
        data, status = {}, "unreadable"
    providers = data.get("model_providers", {})
    providers = providers if isinstance(providers, dict) else {}
    # Whitelist metadata. No base URLs, auth objects, or raw config in results.
    channels = []
    for name, entry in providers.items():
        if not isinstance(entry, dict):
            continue
        env_name = entry.get("env_key")
        channels.append({"provider": name, "env_name": env_name if isinstance(env_name, str) else None,
                         "env_key": bool(os.environ.get(env_name)) if isinstance(env_name, str) else False})
    model = data.get("model")
    facts = {"model": model if isinstance(model, str) else None,
             "model_providers": sorted(providers), "config_status": status}
    return home, codex, facts, channels


def probe(instance="instance", *, refresh=False):
    """Probe local CLIs concurrently (5s each); reuse facts for 24h by mtime.

    API channels come only from configured Codex model_providers with a nonempty
    env_key. Credential presence is refreshed even on cache hits, without CLI calls.
    """
    home, codex, config, channels = _config_facts()
    secret_names = _secret_names(channel["env_name"] for channel in channels if channel["env_name"])
    context = {"home": str(home), "codex_home": str(codex), "path": os.environ.get("PATH", "")}
    cache = Path(instance) / "agents-cache.json"
    agents = None
    if not refresh:
        try:
            age = time.time() - cache.stat().st_mtime
            saved = json.loads(cache.read_text())
            if (0 <= age < CACHE_TTL and saved.get("schema") == 1
                    and saved.get("context") == redact(context, secret_names)
                    and isinstance(saved.get("agents"), dict)
                    and all(isinstance(saved["agents"].get(name), dict) for name in CANDIDATES)):
                agents = saved["agents"]
        except (OSError, ValueError, AttributeError):
            pass
    if agents is None:
        def inspect(name):
            path = find_cli(name)
            directory = codex if name == "codex" else home / CONFIG_DIRS[name]
            facts = {"found": path is not None, "path": path,
                     "version": _version(path, secret_names) if path else None,
                     "config_dir": str(directory), "config_exists": directory.is_dir()}
            if name == "codex":
                facts.update(config)
            if name == "hermes":
                try:
                    facts["profiles"] = sorted(item.name for item in (directory / "profiles").iterdir())
                except OSError:
                    facts["profiles"] = []
            return name, facts

        with ThreadPoolExecutor(max_workers=len(CANDIDATES)) as executor:
            agents = dict(executor.map(inspect, CANDIDATES))
        agents = redact(agents, secret_names)
        write_private_json(cache, {"schema": 1, "context": redact(context, secret_names), "agents": agents})
    try:
        from .model_access import ModelAccess
        for row in ModelAccess(instance).records():
            agents[row["id"]] = {"found": True, "path": None, "version": row["model"],
                                 "config_dir": None, "config_exists": True,
                                 "custom": True, "env_key": True}
    except Exception:
        pass
    for name in CANDIDATES:
        agents[name]["env_key"] = any(bool(os.environ.get(key)) for key in ENV_KEYS[name])
    agents["codex"]["env_key"] |= any(channel["env_key"] for channel in channels)
    agents["api"] = {"found": any(channel["env_key"] for channel in channels),
                     "path": None, "version": None, "config_dir": str(codex),
                     "config_exists": codex.is_dir(), "model": config["model"],
                     "providers": [{"provider": entry["provider"], "env_key": entry["env_key"]}
                                   for entry in channels],
                     "env_key": any(channel["env_key"] for channel in channels)}
    return redact(agents, secret_names)
