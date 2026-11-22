"""Real streamed decisions. Models have no tools, credentials or mutation access."""
from __future__ import annotations

import hashlib
import fcntl
import json
import os
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from .agent_channels import CHANNELS, resolve, supports_json_schema
from .agent_env import resolve as env_overrides
from .cli_agents import AGENTS as CLI_AGENTS, CLITransport
from .ai_schema import SCHEMAS, check_order, check_plan, validate
from .role_settings import ROLE_PROMPTS
from .core import PolicyError, now
from .mutate import canonical, payload_hash
from .transports import (StreamTransport, OpenAIStreamTransport, AnthropicStreamTransport,
                         TransportError, strict_json)

MODEL = "gpt-6.1-sol"

AGENT_DEFAULT_MODEL = {agent: entry["default_model"] for agent, entry in CHANNELS.items()}
RULES = ROLE_PROMPTS


def runtime_context(workspace, role, now=None):
    """Explicit local facts only; never copy environment or credentials.

    `now` is frozen once per run and passed in: a value recomputed per model
    call makes the plan's timestamp stale by the time 门下省 reviews it, which
    produced an endless reject/revise loop (the reviewer always saw a newer
    clock than the plan quoted).
    """
    local = now if now is not None else datetime.now().astimezone()
    context = {"now": local.isoformat(), "workspace": str(Path(workspace).resolve())}
    if role == "gongbu":
        context.update(timezone=str(local.tzinfo), role=role,
                       limits={"max_file_bytes": 262144, "no_network": True,
                               "no_commands": True})
    return context


class DecisionClient:
    def __init__(self, directory, transport=None, max_calls=12, bindings=None, instance=None):
        if type(max_calls) is not int or not 1 <= max_calls <= 12:
            raise PolicyError("round call limit must be 1..12")
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=False)
        self._lock_round()
        self.bindings = bindings if bindings is not None else {}
        self.instance = instance
        self.transports = {}
        self._injected_transport = transport is not None
        self.transport = transport if transport is not None else (None if self.bindings else StreamTransport())
        self.max_calls = max_calls
        self.calls = 0
        (self.directory / "round.json").write_text(canonical({"max_calls": max_calls}))

    def _lock_round(self):
        self._lock = (self.directory / ".round.lock").open("a")
        try:
            fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self._lock.close()
            raise PolicyError("AI round already has an active controller") from None

    @classmethod
    def resume(cls, directory, transport=None, bindings=None, instance=None):
        """Explicit continuation consumes the SAME durable 12-call allowance."""
        client = cls.__new__(cls)
        client.directory = Path(directory).resolve(strict=True)
        client._lock_round()
        calls = [strict_json(line) for line in (client.directory / "calls.jsonl").read_text().splitlines()]
        client.calls = len(calls)
        policy = client.directory / "round.json"
        # Early evidence rounds predate this metadata and used the fixed 12 cap.
        client.max_calls = strict_json(policy.read_text())["max_calls"] if policy.exists() else 12
        if type(client.max_calls) is not int or not 1 <= client.max_calls <= 12:
            raise PolicyError("invalid saved round limit")
        if client.calls > client.max_calls:
            raise PolicyError("existing AI round exceeds call limit")
        client.bindings = bindings if bindings is not None else {}
        client.instance = instance
        client.transports = {}
        client._injected_transport = transport is not None
        client.transport = transport if transport is not None else (None if client.bindings else StreamTransport())
        return client

    def decide(self, step, context):
        schema = SCHEMAS[step]
        binding = self.bindings.get(step) or {}
        metadata = {}
        transport = self.transport if self._injected_transport else None
        if binding:
            agent = binding.get("agent")
            protocol, base, key_env, model = resolve(agent, binding.get("model"), instance=self.instance)
            overrides = env_overrides(agent, self.instance)
            if overrides["base"]:
                base = overrides["base"]
            if overrides["model"]:
                model = overrides["model"]
            key_value = overrides["key"]
            metadata = {"agent": agent, "protocol": protocol,
                        "endpoint_host": urlsplit(base).hostname if base else "local-cli"}
            if transport is None:
                if agent in CLI_AGENTS:
                    if agent not in self.transports:
                        self.transports[agent] = CLITransport(agent, model, env=overrides["env"])
                else:
                    if agent not in self.transports:
                        cls = AnthropicStreamTransport if protocol == "anthropic" else OpenAIStreamTransport
                        channel = cls(base, key_env, model,
                                      allow_loopback_http=str(agent).startswith("custom-"),
                                      key_value=key_value)
                        if isinstance(channel, OpenAIStreamTransport):
                            channel.json_schema = supports_json_schema(agent, instance=self.instance)
                        self.transports[agent] = channel
                transport = self.transports[agent]
        else:
            model = MODEL
            if transport is None:
                if self.transport is None:
                    self.transport = StreamTransport()
                transport = self.transport
        if not isinstance(model, str) or not model.strip():
            raise PolicyError("role model must be nonempty")
        error = None
        for attempt in range(2):
            if self.calls >= self.max_calls:
                raise PolicyError("MODEL_CALL_LIMIT: round exhausted")
            messages = [{"role": "system", "content": RULES[step] +
                         " Return ONLY a JSON object matching this schema: " + canonical(schema)},
                        {"role": "user", "content": canonical({"input": context, "validation_error": error})}]
            request = {"model": model, "stream": True, "reasoning_effort": "low",
                       "max_completion_tokens": 4096,
                       "stream_options": {"include_usage": True}, "messages": messages,
                       "response_format": {"type": "json_schema", "json_schema": {
                           "name": step, "strict": True, "schema": schema}}}
            if isinstance(transport, AnthropicStreamTransport):
                request = transport.prepare(request)
            if len(canonical(request).encode()) > 60000:
                raise PolicyError("MODEL_INPUT_LIMIT: request exceeds 60000 bytes")
            self.calls += 1
            stem = f"{self.calls:02d}-{step}"
            prompt_path = self.directory / (stem + ".request.json")
            prompt_path.write_text(canonical(request))
            record = {**metadata, "step": step, "attempt": attempt + 1, "model": model,
                      "model_sha256": payload_hash(model), "prompt_sha256": payload_hash(request),
                      "output_sha256": hashlib.sha256(b"").hexdigest(), "decision": None,
                      "at": now(), "tokens": None, "cost_usd": None, "pricing": "unknown",
                      "http_status": None, "status": "started", "request_path": prompt_path.name}
            self._append(record, "calls.jsonl")  # durable attempted-call count before sending
            started = time.monotonic()
            try:
                result = transport(request, self.directory / (stem + ".sse"))
            except Exception as exc:
                record.update(status="transport_error", error=type(exc).__name__,
                              elapsed_sec=round(time.monotonic() - started, 3))
                if metadata:
                    status = exc.http_status if isinstance(exc, TransportError) else 0
                    record["http_status"] = status
                    self._append(record)
                    raise PolicyError(f"{step}: {agent}/{protocol} {metadata['endpoint_host']} HTTP {status}") from None
                self._append(record)
                raise PolicyError(f"{step}: transport failed; no fallback ({exc})") from exc
            raw = result["output"]
            (self.directory / (stem + ".output.txt")).write_text(raw)
            record.update(output_sha256=hashlib.sha256(raw.encode()).hexdigest(),
                          tokens=result.get("usage"), returned_model=result.get("model"),
                          returned_model_sha256=payload_hash(result.get("model")),
                          http_status=result.get("http_status"), elapsed_sec=result.get("elapsed_sec"),
                          output_path=stem + ".output.txt")
            try:
                value = strict_json(raw)
                validate(value, schema)
                if step == "zhongshu":
                    check_plan(value)
                if step == "shangshu":
                    check_order(value["order"], context["plan"])
                if step == "menxia" and value["verdict"] == "reject" and not value["required_changes"]:
                    raise PolicyError("rejection requires actionable changes")
                record.update(status="valid", decision=value)
            except (ValueError, TypeError, KeyError) as exc:
                error = str(exc)
                record.update(status="invalid", error=error)
                self._append(record)
                if attempt == 1:
                    raise PolicyError(f"{step}: invalid schema/semantics after 2 attempts: {error}") from exc
                continue
            self._append(record)
            return value
        raise AssertionError("unreachable")

    def _append(self, record, name="decisions.jsonl"):
        with (self.directory / name).open("a") as output:
            output.write(canonical(record) + "\n")
            output.flush()
            os.fsync(output.fileno())

    def summary(self):
        path = self.directory / "decisions.jsonl"
        records = [strict_json(line) for line in path.read_text().splitlines()] if path.exists() else []
        usages = [record["tokens"] for record in records]
        total = None
        if usages and all(isinstance(usage, dict) and type(usage.get("total_tokens")) is int
                          for usage in usages):
            total = sum(usage["total_tokens"] for usage in usages)
        return {"calls": self.calls, "steps": records, "total_cost_usd": None,
                "pricing": "unknown", "tokens": usages, "total_tokens": total}
