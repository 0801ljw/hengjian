"""Explicit, budgeted live Agent probe — one tiny real call, never automatic.

The probe exists because static readiness can say "ready" while the key, model
or CLI is actually unusable. It is admin-only, has a fixed prompt, a short
cooldown and an hour cap; it never accepts caller-authored messages.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import tempfile
import time

from .agent_channels import resolve
from .agents_probe import write_private_json
from .cli_agents import AGENTS as CLI_AGENTS, CLITransport
from .core import PolicyError
from .transports import AnthropicStreamTransport, OpenAIStreamTransport

PROMPT = "Reply with exactly OK. Do not use tools."
MAX_HOUR = 5
COOLDOWN = 30
STATE = "live-probe.json"


def _clean(text):
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", " ", str(text or ""))
    return text.strip()[:200]


def _load(instance):
    path = Path(instance) / STATE
    try:
        import json
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return []
    rows = data.get("events") if isinstance(data, dict) else None
    return [row for row in rows or [] if isinstance(row, (int, float))]


def _save(instance, events):
    write_private_json(Path(instance) / STATE, {"events": events[-MAX_HOUR:]})


def probe(agent, model=None, instance=None):
    """Run one minimal call; return a bounded result or raise PolicyError."""
    now = time.time()
    events = _load(instance) if instance else []
    recent = [stamp for stamp in events if now - stamp < 3600]
    if recent and now - recent[-1] < COOLDOWN:
        raise PolicyError(f"实测冷却中，请 {int(COOLDOWN - (now - recent[-1]))} 秒后再试")
    if len(recent) >= MAX_HOUR:
        raise PolicyError(f"每小时最多实测 {MAX_HOUR} 次，请稍后再试")
    protocol, base, key_env, resolved_model = resolve(agent, model, instance=instance)
    model_name = resolved_model or "default"
    if agent in CLI_AGENTS:
        transport = CLITransport(agent, model_name)
    else:
        if not base:
            raise PolicyError("该 Agent 没有可用接口")
        cls = AnthropicStreamTransport if protocol == "anthropic" else OpenAIStreamTransport
        transport = cls(base, key_env, model_name,
                        allow_loopback_http=str(agent).startswith("custom-"))
    request = {"model": model_name, "stream": True, "max_completion_tokens": 16,
               "messages": [{"role": "user", "content": PROMPT}]}
    if isinstance(transport, AnthropicStreamTransport):
        request = transport.prepare(request)
    started = time.monotonic()
    # Count the attempt before the call: a failed paid call still consumes
    # budget and must not be loopholed by retrying.
    if instance:
        _save(instance, [*recent, now])
    try:
        with tempfile.TemporaryDirectory(prefix="dutang-live-") as directory:
            result = transport(request, Path(directory) / "probe.sse")
    except PolicyError as exc:
        raise PolicyError(str(exc)[:300]) from None
    except Exception:
        raise PolicyError("实测调用失败") from None
    reply = _clean(result.get("output"))
    if not reply:
        raise PolicyError("实测调用没有返回内容")
    return {"ok": True, "agent": agent, "model": model_name,
            "reply": reply, "elapsed_sec": round(time.monotonic() - started, 3),
            "usage": result.get("usage")}
