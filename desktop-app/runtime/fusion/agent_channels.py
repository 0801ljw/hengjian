"""Validated agent-owned channels. Configuration stores names, never credentials."""
from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path
from urllib.parse import urlsplit

from .cli_agents import AGENTS as CLI_AGENTS, resolve as resolve_cli
from .core import PolicyError

CHANNELS = {
    "codex": {"protocol": "openai", "base_url": "https://penguinapi.vip/v1",
              "key_env": "PENGUIN_GPT_API_KEY", "default_model": "gpt-6.1-sol"},
    "hermes": {"protocol": "openai", "base_url": "https://penguinapi.vip/v1",
               "key_env": "PENGUIN_GPT_API_KEY", "default_model": "gpt-6.1-terra"},
    # deepseek rejects response_format json_schema (HTTP 400); it only offers
    # json_object, which additionally requires the word "json" in the prompt.
    "pi": {"protocol": "openai", "base_url": "https://api.deepseek.com/v1",
           "key_env": "DEEPSEEK_API_KEY", "default_model": "deepseek-flash",
           "json_schema": False},
    "claude": {"protocol": "anthropic", "base_url": "https://modelflare.dev",
               "key_env": "MODELFLARE_CLAUDE_API_KEY", "default_model": "claude-sonnet-5-5"},
}


def https_url(value):
    """Refuse credentials, query strings, fragments and malformed authorities."""
    try:
        if not isinstance(value, str) or any(c.isspace() or ord(c) < 32 for c in value):
            raise ValueError
        parsed = urlsplit(value)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.query or parsed.fragment
                or "\\" in value):
            raise ValueError
        parsed.port  # Validate port syntax/range without exposing the supplied URL.
    except ValueError:
        raise PolicyError("channel base_url requires HTTPS without credentials/query/fragment") from None
    return parsed


def _known(agent):
    if not isinstance(agent, str) or agent not in CHANNELS:
        raise PolicyError("unknown agent; known agents: " + ", ".join(sorted(CHANNELS)))


ALLOWED_FIELDS = {"protocol", "base_url", "key_env", "default_model", "json_schema"}


def _validate(entry):
    if not isinstance(entry, dict) or set(entry) - ALLOWED_FIELDS:
        raise PolicyError("channel has unknown fields or is not an object")
    if "json_schema" in entry and not isinstance(entry["json_schema"], bool):
        raise PolicyError("channel json_schema must be a boolean")
    for key, value in entry.items():
        if key == "json_schema":
            continue
        if not isinstance(value, str) or not value.strip() or value != value.strip():
            raise PolicyError("channel " + key + " must be a nonempty string")
        if key == "protocol" and value not in ("openai", "anthropic"):
            raise PolicyError("channel protocol must be openai or anthropic")
        if key == "base_url":
            https_url(value)
        if key == "key_env" and not re.fullmatch(r"[A-Z_][A-Z0-9_]*", value):
            raise PolicyError("channel key_env must be an environment variable name")
        if key == "default_model" and (len(value) > 200 or any(ord(c) < 32 for c in value)):
            raise PolicyError("channel default_model is invalid")


def load(instance=None):
    channels = {agent: dict(entry) for agent, entry in CHANNELS.items()}
    if instance is not None:
        path = Path(instance) / "agents.json"
        try:
            with path.open() as source:
                if stat.S_IMODE(os.fstat(source.fileno()).st_mode) != 0o600:
                    raise PolicyError("agents.json must have mode 0600")
                # Reject duplicate keys rather than silently accepting the last one.
                def unique(pairs):
                    result = {}
                    for key, value in pairs:
                        if key in result:
                            raise ValueError
                        result[key] = value
                    return result
                data = json.load(source, object_pairs_hook=unique)
        except FileNotFoundError:
            data = {}
        except PolicyError:
            raise
        except (OSError, ValueError):
            raise PolicyError("cannot load agents.json") from None
        if not isinstance(data, dict) or set(data) - {"channels"}:
            raise PolicyError("agents.json requires only channels")
        overrides = data.get("channels", {})
        if not isinstance(overrides, dict):
            raise PolicyError("channels must be an object")
        for agent, entry in overrides.items():
            _known(agent)
            _validate(entry)
            channels[agent].update(entry)
    try:
        from .model_access import ModelAccess
        channels.update(ModelAccess(instance).channels())
    except Exception:
        # A malformed optional model file must not take down built-in channels;
        # the model API reports its own validation error.
        pass
    for entry in channels.values():
        _validate(entry)
    return channels


def resolve(agent, model=None, *, instance=None):
    if isinstance(agent, str) and agent in CLI_AGENTS:
        return resolve_cli(agent, model)
    entries = load(instance)
    if not isinstance(agent, str) or agent not in entries:
        raise PolicyError("unknown agent; known agents: " + ", ".join(sorted(CHANNELS)))
    entry = entries[agent]
    if model is not None and not isinstance(model, str):
        raise PolicyError("role model must be a string")
    model = (model or "").strip() or entry["default_model"]
    return entry["protocol"], entry["base_url"], entry["key_env"], model


def credential_present(key_env):
    """Whether the channel's credential variable is set and non-empty."""
    return bool(os.environ.get(key_env, "").strip())


def validate_binding(agent, model=None, *, instance=None, require_credential=False):
    """Validate that a binding names a known channel with a usable endpoint.

    The credential is only enforced when asked. Setup must stay possible on a
    machine whose keys are not configured yet - the workbench still has to be
    reachable, and the missing credential is reported as a warning here and as
    a named error when a run actually needs the channel.
    """
    channel = resolve(agent, model, instance=instance)
    if require_credential and not credential_present(channel[2]):
        raise PolicyError("missing credential environment variable: " + channel[2])
    return channel


def supports_json_schema(agent, *, instance=None):
    """Whether the agent's channel accepts response_format json_schema."""
    entries = load(instance)
    if not isinstance(agent, str) or agent not in entries:
        raise PolicyError("unknown agent; known agents: " + ", ".join(sorted(CHANNELS)))
    return bool(entries[agent].get("json_schema", True))
