"""Per-agent generic model environment with provider-specific mapping.

Stores one 0600 file per instance. The UI only ever needs LLM_API_KEY /
LLM_BASE_URL / LLM_MODEL; adapters receive the native names they already read.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat

from .agent_channels import CHANNELS
from .agents_probe import write_private_json
from .cli_agents import AGENTS as CLI_AGENTS
from .core import PolicyError

FILE = "agent-env.json"
FIELDS = ("LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL")


def _path(instance):
    return Path(instance) / FILE


def load(instance):
    path = _path(instance)
    try:
        if stat.S_IMODE(path.stat().st_mode) != 0o600:
            raise PolicyError("agent-env.json must have mode 0600")
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except PolicyError:
        raise
    except (OSError, ValueError):
        raise PolicyError("cannot read agent-env.json") from None
    if not isinstance(data, dict):
        raise PolicyError("agent-env.json must be an object")
    result = {}
    for agent, values in data.items():
        if not isinstance(values, dict):
            continue
        result[agent] = {key: str(values[key]) for key in FIELDS if isinstance(values.get(key), str)}
    return result


def public(instance):
    return {agent: {key: ("***" if key == "LLM_API_KEY" and value else value)
                    for key, value in values.items()}
            for agent, values in load(instance).items()}


def save(instance, agent, values, *, instance_name=None):
    if not isinstance(agent, str) or agent not in CHANNELS and agent not in CLI_AGENTS:
        raise PolicyError("unknown agent")
    if not isinstance(values, dict):
        raise PolicyError("values must be an object")
    clean = {}
    for key in FIELDS:
        value = values.get(key)
        if value is None or value == "":
            continue
        if not isinstance(value, str) or len(value) > 4000 or any(ord(c) < 32 for c in value):
            raise PolicyError("invalid environment value")
        clean[key] = value
    current = load(instance)
    if "LLM_API_KEY" not in clean and current.get(agent, {}).get("LLM_API_KEY"):
        clean["LLM_API_KEY"] = current[agent]["LLM_API_KEY"]
    if clean:
        current[agent] = clean
    else:
        current.pop(agent, None)
    write_private_json(_path(instance), current)
    return public(instance).get(agent, {})


def resolve(agent, instance=None):
    """Return native key/base/model and a CLI env map for one agent."""
    values = load(instance).get(agent, {}) if instance else {}
    if not values:
        return {"key": None, "base": None, "model": None, "env": {}}
    base = values.get("LLM_BASE_URL") or ""
    if agent in CHANNELS:
        key_name = CHANNELS[agent]["key_env"]
        model_name = {"codex": "CODEX_MODEL", "claude": "ANTHROPIC_MODEL",
                      "pi": "PI_MODEL", "hermes": "OPENCLAW_MODEL"}.get(agent, "LLM_MODEL")
        env = {}
        if values.get("LLM_API_KEY"):
            env[key_name] = values["LLM_API_KEY"]
        if base:
            env["OPENAI_BASE_URL"] = base
        if values.get("LLM_MODEL"):
            env[model_name] = values["LLM_MODEL"]
        return {"key": values.get("LLM_API_KEY"), "base": base or None,
                "model": values.get("LLM_MODEL"), "env": env}
    env = {}
    anthropic = "anthropic" in base.lower()
    mapping = {
        "gemini": ("GEMINI_API_KEY", "GOOGLE_GEMINI_BASE_URL", "GEMINI_MODEL"),
        "qwen": ("DASHSCOPE_API_KEY", "OPENAI_BASE_URL", "QWEN_MODEL"),
        "opencode": ("ANTHROPIC_API_KEY" if anthropic else "OPENAI_API_KEY",
                     "ANTHROPIC_BASE_URL" if anthropic else "OPENAI_BASE_URL", "OPENCODE_MODEL"),
        "openclaw": ("ANTHROPIC_API_KEY" if anthropic else "OPENAI_API_KEY",
                     "ANTHROPIC_BASE_URL" if anthropic else "OPENAI_BASE_URL", "OPENCLAW_MODEL"),
        "copilot": ("GITHUB_TOKEN", None, "COPILOT_MODEL"),
    }
    key_name, base_name, model_name = mapping.get(agent, (None, None, None))
    if values.get("LLM_API_KEY") and key_name:
        env[key_name] = values["LLM_API_KEY"]
    if base and base_name:
        env[base_name] = base
    if values.get("LLM_MODEL") and model_name:
        env[model_name] = values["LLM_MODEL"]
    return {"key": values.get("LLM_API_KEY"), "base": base or None,
            "model": values.get("LLM_MODEL"), "env": env}
