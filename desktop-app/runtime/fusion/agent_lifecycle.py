"""Opt-in process lifecycle for agents that are actually daemons.

A CLI agent is invoked per Issue and has no lifecycle here. An operator may
configure start/stop argv for a real daemon; commands are executed directly
(no shell), with a minimal environment and a private log/pid file.
"""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import time
from pathlib import Path

from .core import PolicyError

SAFE = re.compile(r"[A-Za-z0-9_.-]{1,100}")
ALLOWED = {"start", "stop", "restart", "max_restarts", "restart_backoff_seconds", "health"}


def _check(agent):
    if not isinstance(agent, str) or not SAFE.fullmatch(agent):
        raise PolicyError("invalid agent id")


def _root(instance, agent):
    _check(agent)
    root = Path(instance) / "agents" / agent
    root.mkdir(parents=True, exist_ok=True)
    root.chmod(0o700)
    return root


def config_path(instance, agent):
    return _root(instance, agent) / "lifecycle.json"


def pid_path(instance, agent):
    return _root(instance, agent) / "lifecycle.pid"


def log_path(instance, agent):
    return _root(instance, agent) / "lifecycle.log"


def state_path(instance, agent):
    return _root(instance, agent) / "lifecycle-state.json"


def _read_state(instance, agent):
    try:
        data = json.loads(state_path(instance, agent).read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_state(instance, agent, data):
    path = state_path(instance, agent)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    tmp.chmod(0o600)
    tmp.replace(path)


def _validate_argv(value, field):
    if not isinstance(value, list) or not 1 <= len(value) <= 32:
        raise PolicyError(f"{field} must be a command array")
    if any(not isinstance(item, str) or not item or "\x00" in item for item in value):
        raise PolicyError(f"{field} contains an invalid argument")
    if not os.path.isabs(value[0]):
        raise PolicyError(f"{field}[0] must be an absolute path")


def load_config(instance, agent):
    try:
        data = json.loads(config_path(instance, agent).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or set(data) - ALLOWED:
        return None
    try:
        _validate_argv(data.get("start"), "start")
        if "stop" in data:
            _validate_argv(data["stop"], "stop")
        if "health" in data:
            _validate_argv(data["health"], "health")
        if data.get("restart", "never") not in ("never", "always", "on-failure"):
            return None
        if not isinstance(data.get("max_restarts", 3), int) or not 0 <= data["max_restarts"] <= 10:
            return None
        if not isinstance(data.get("restart_backoff_seconds", 30), int) or not 5 <= data["restart_backoff_seconds"] <= 3600:
            return None
    except PolicyError:
        return None
    return {"start": data["start"], "stop": data.get("stop"),
            "health": data.get("health"),
            "restart": data.get("restart", "never"),
            "max_restarts": data.get("max_restarts", 3),
            "restart_backoff_seconds": data.get("restart_backoff_seconds", 30)}


def save_config(instance, agent, data):
    if not isinstance(data, dict) or set(data) - ALLOWED:
        raise PolicyError("lifecycle contains unknown fields")
    _validate_argv(data.get("start"), "start")
    if "stop" in data:
        _validate_argv(data["stop"], "stop")
    if "health" in data:
        _validate_argv(data["health"], "health")
    restart = data.get("restart", "never")
    if restart not in ("never", "always", "on-failure"):
        raise PolicyError("restart must be never, always or on-failure")
    max_restarts = data.get("max_restarts", 3)
    backoff = data.get("restart_backoff_seconds", 30)
    if type(max_restarts) is not int or not 0 <= max_restarts <= 10:
        raise PolicyError("max_restarts must be 0..10")
    if type(backoff) is not int or not 5 <= backoff <= 3600:
        raise PolicyError("restart_backoff_seconds must be 5..3600")
    value = {"start": data["start"], "stop": data.get("stop"),
             "health": data.get("health"), "restart": restart,
             "max_restarts": max_restarts, "restart_backoff_seconds": backoff}
    path = config_path(instance, agent)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps({key: val for key, val in value.items() if val is not None},
                              ensure_ascii=False, indent=2))
    tmp.chmod(0o600)
    tmp.replace(path)
    return value


def _pid(instance, agent):
    try:
        value = int(pid_path(instance, agent).read_text().strip())
    except (OSError, ValueError):
        return None
    if value <= 0:
        return None
    try:
        os.kill(value, 0)
    except ProcessLookupError:
        return None
    except PermissionError:
        return value
    return value


def _process(instance, agent):
    pid = _pid(instance, agent)
    if pid is None:
        return {"pid": None, "running": False, "exit_code": None}
    try:
        reaped, status_value = os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        return {"pid": pid, "running": True, "exit_code": None}
    if reaped == pid:
        pid_path(instance, agent).unlink(missing_ok=True)
        try:
            code = os.waitstatus_to_exitcode(status_value)
        except (ValueError, OverflowError):
            code = None
        return {"pid": None, "running": False, "exit_code": code}
    return {"pid": pid, "running": True, "exit_code": None}


def health(instance, agent):
    config = load_config(instance, agent)
    if not config or not config.get("health"):
        return None
    env = {key: os.environ[key] for key in ("PATH", "HOME", "LANG", "TMPDIR")
           if key in os.environ}
    try:
        done = subprocess.run(config["health"], stdin=subprocess.DEVNULL,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              env=env, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


def status(instance, agent):
    config = load_config(instance, agent)
    process = _process(instance, agent)
    state = _read_state(instance, agent)
    return {"agent": agent, "configured": bool(config), "running": process["running"],
            "pid": process["pid"], "log": str(log_path(instance, agent)) if config else None,
            "paused": bool(state.get("paused")), "attempts": int(state.get("attempts") or 0),
            "exit_code": process["exit_code"],
            "health": health(instance, agent) if process["running"] else None}


def start(instance, agent):
    config = load_config(instance, agent)
    if not config:
        raise PolicyError("no lifecycle configured for this agent")
    pid = _pid(instance, agent)
    if pid:
        return {**status(instance, agent), "started": False}
    log = log_path(instance, agent)
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("ab") as output:
        log.chmod(0o600)
        env = {key: os.environ[key] for key in ("PATH", "HOME", "LANG", "TMPDIR")
               if key in os.environ}
        try:
            process = subprocess.Popen(config["start"], stdin=subprocess.DEVNULL,
                                       stdout=output, stderr=subprocess.STDOUT,
                                       env=env, start_new_session=True)
        except OSError as exc:
            raise PolicyError(f"start failed: {exc}") from None
    time.sleep(0.2)
    if process.poll() is not None:
        raise PolicyError("started process exited immediately; see lifecycle.log")
    pid_path(instance, agent).write_text(str(process.pid))
    pid_path(instance, agent).chmod(0o600)
    state = _read_state(instance, agent)
    state.update(paused=False, attempts=0)
    _write_state(instance, agent, state)
    return {**status(instance, agent), "started": True}


def stop(instance, agent):
    config = load_config(instance, agent)
    if not config:
        raise PolicyError("no lifecycle configured for this agent")
    pid = _pid(instance, agent)
    if pid is None:
        pid_path(instance, agent).unlink(missing_ok=True)
        return {**status(instance, agent), "stopped": False}
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    else:
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        os.waitpid(pid, os.WNOHANG)
    except (ChildProcessError, ProcessLookupError):
        pass
    pid_path(instance, agent).unlink(missing_ok=True)
    state = _read_state(instance, agent)
    state.update(paused=True)
    _write_state(instance, agent, state)
    return {**status(instance, agent), "stopped": True}


def _alert(instance, kind, agent, title, body):
    try:
        from .alerts import raise_alert
        raise_alert(instance, kind, agent, title, body)
    except Exception:
        pass


def supervise(instance, now=None):
    """Restart opted-in daemons according to policy and health."""
    import time
    now = time.time() if now is None else float(now)
    root = Path(instance) / "agents"
    if not root.is_dir():
        return []
    actions = []
    for directory in sorted(root.iterdir()):
        if not directory.is_dir():
            continue
        agent = directory.name
        config = load_config(instance, agent)
        if not config or config.get("restart") == "never":
            continue
        state = _read_state(instance, agent)
        if state.get("paused"):
            continue
        process = _process(instance, agent)
        if process["exit_code"] is not None:
            state["last_exit_code"] = process["exit_code"]
        healthy = True
        if process["running"] and config.get("health"):
            healthy = bool(health(instance, agent))
            if not healthy:
                _alert(instance, "lifecycle_unhealthy", agent,
                       f"Agent {agent} health check failed",
                       "health command returned non-zero; supervisor will restart according to policy")
                stop(instance, agent)
                process["running"] = False
        if process["running"] and healthy:
            continue
        if config["restart"] == "on-failure" and process["exit_code"] == 0:
            _write_state(instance, agent, {**state, "next_at": now})
            continue
        if int(state.get("attempts") or 0) >= int(config.get("max_restarts", 3)):
            _alert(instance, "lifecycle_exhausted", agent,
                   f"Agent {agent} restart limit reached",
                   f"attempts={state.get('attempts')} max={config.get('max_restarts', 3)}")
            continue
        if float(state.get("next_at") or 0) > now:
            continue
        attempts = int(state.get("attempts") or 0) + 1
        backoff = int(config.get("restart_backoff_seconds", 30))
        try:
            start(instance, agent)
            _write_state(instance, agent, {"paused": False, "attempts": attempts,
                                           "next_at": now + backoff,
                                           "last_exit_code": state.get("last_exit_code")})
            actions.append({"agent": agent, "action": "started", "attempts": attempts})
        except Exception as exc:
            _write_state(instance, agent, {"paused": False, "attempts": attempts,
                                           "next_at": now + backoff,
                                           "last_exit_code": state.get("last_exit_code"),
                                           "error": str(exc)[:300]})
            actions.append({"agent": agent, "action": "failed", "attempts": attempts})
            if attempts >= int(config.get("max_restarts", 3)):
                _alert(instance, "lifecycle_exhausted", agent,
                       f"Agent {agent} restart limit reached",
                       f"attempts={attempts} last_error={str(exc)[:300]}")
    return actions



def restart(instance, agent):
    stop(instance, agent)
    return start(instance, agent)
