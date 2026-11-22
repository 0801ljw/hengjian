"""SSE transports; legacy single-channel wire behavior is preserved."""
from __future__ import annotations

import json
import os
import time
import tomllib
import urllib.error
import urllib.request
from pathlib import Path

from .agent_channels import https_url
from .core import PolicyError
from .mutate import canonical

MAX_STREAM_BYTES = 2_000_000
STREAM_TIMEOUT = 90
MAX_STREAM_SECONDS = 240  # long reviewer streams; still bounded


def strip_single_code_fence(raw):
    """Return the body of the first fenced block, else the payload unchanged.

    Anthropic has no response_format, so a correct answer still arrives as
    ```json ... ``` and may carry a one-line closing remark. Unwrapping exactly
    one fenced block keeps the parser strict about the JSON itself: payloads
    without a fence, or with a fence that does not parse, are still rejected.
    """
    if not isinstance(raw, str):
        return raw
    text = raw.strip()
    if not text.startswith("```"):
        return raw
    lines = text.splitlines()
    closing = next((i for i, line in enumerate(lines[1:], start=1) if line.strip() == "```"), None)
    if closing is None or closing < 2:
        return raw
    return "\n".join(lines[1:closing])


def strict_json(raw):
    # Bundle manifests are parsed from bytes; fence unwrapping is a text concern.
    if isinstance(raw, str):
        raw = strip_single_code_fence(raw)

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate JSON field")
            result[key] = value
        return result
    def invalid(value):
        raise ValueError("non-finite JSON number")
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid)


class StreamTransport:
    """Use only the already configured penguin channel; never switch providers."""
    def __init__(self, config=None):
        config = config or Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "config.toml"
        data = tomllib.loads(Path(config).read_text())
        provider = data["model_providers"]["penguinapi"]
        self.endpoint = provider["base_url"].rstrip("/") + "/chat/completions"
        if not self.endpoint.startswith("https://"):
            raise PolicyError("configured provider requires HTTPS")
        self.key = os.environ.get(provider["env_key"])
        if not self.key:
            raise PolicyError("configured penguin credential unavailable")

    def __call__(self, request, stream_path):
        req = urllib.request.Request(self.endpoint, data=canonical(request).encode(),
                                     headers={"Authorization": "Bearer " + self.key,
                                              "Content-Type": "application/json"})
        # No redirects: never forward a bearer credential to another origin.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        started = time.monotonic()
        output, usage, model, finished, done = [], None, None, False, False
        try:
            with urllib.request.build_opener(NoRedirect).open(req, timeout=90) as response, stream_path.open("wb") as log:
                status = response.status
                if "text/event-stream" not in response.headers.get("Content-Type", ""):
                    raise PolicyError("provider did not return SSE")
                size = 0
                for raw in response:
                    size += len(raw)
                    if size > 2_000_000 or time.monotonic() - started > 120:
                        raise PolicyError("stream size/time limit")
                    log.write(raw)
                    line = raw.decode("utf-8").strip()
                    if line.startswith("event:") and line[6:].strip() == "error":
                        raise PolicyError("provider stream error")
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        done = True
                        break
                    event = strict_json(data)
                    if event.get("error"):
                        raise PolicyError("provider stream error")
                    model = event.get("model", model)
                    usage = event.get("usage") or usage
                    for choice in event.get("choices", []):
                        if choice.get("index", 0) != 0:
                            raise PolicyError("multiple choices refused")
                        delta = choice.get("delta", {})
                        if delta.get("refusal"):
                            raise PolicyError("model refused")
                        if delta.get("content"):
                            output.append(delta["content"])
                        finish = choice.get("finish_reason")
                        if finish is not None:
                            if finish != "stop":
                                raise PolicyError("incomplete model response")
                            finished = True
                if not done or not finished or not output:
                    raise PolicyError("incomplete SSE stream")
        except urllib.error.HTTPError as exc:
            raise PolicyError(f"provider HTTP {exc.code}; no fallback") from None
        except (OSError, ValueError) as exc:
            raise PolicyError(f"provider transport failure: {type(exc).__name__}") from None
        return {"output": "".join(output), "usage": usage, "model": model,
                "http_status": status, "elapsed_sec": round(time.monotonic() - started, 3)}


class TransportError(PolicyError):
    def __init__(self, host, status):
        self.http_status = status
        super().__init__(f"{host} HTTP {status}")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class OpenAIStreamTransport:
    # Strict-schema channels keep response_format json_schema; channels that only
    # offer json_object are downgraded and validated by our own strict parser.
    json_schema = True
    """Parameterized OpenAI SSE, with bounded reads and redacted diagnostics."""
    protocol = "openai"
    suffix = "/chat/completions"

    def __init__(self, base_url, key_env, model=None, *, allow_loopback_http=False, key_value=None):
        try:
            parsed = https_url(base_url)
        except PolicyError:
            if not allow_loopback_http:
                raise
            from urllib.parse import urlsplit
            parsed = urlsplit(base_url)
            if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
                raise PolicyError("model endpoint requires HTTPS or loopback HTTP") from None
        self.endpoint_host = parsed.hostname
        self.endpoint = base_url.rstrip("/") + self.suffix
        self.key_env = key_env
        self.key = key_value or os.environ.get(key_env, "")
        self.model = model
        if not self.key.strip():
            raise PolicyError("missing credential environment variable: " + key_env)

    def prepare(self, request):
        request = {**request, "model": request.get("model") or self.model}
        if not self.json_schema and "response_format" in request:
            request["response_format"] = {"type": "json_object"}
        return request

    def _headers(self, key):
        return {"Authorization": "Bearer " + key, "Content-Type": "application/json"}

    def _events(self, response, log, started, key):
        """Read SSE frames, including CRLF, comments and multiline data."""
        size, name, lines = 0, "", []
        while True:
            raw = response.readline(MAX_STREAM_BYTES + 1)
            if not raw:
                return
            size += len(raw)
            if size > MAX_STREAM_BYTES or time.monotonic() - started > MAX_STREAM_SECONDS:
                raise ValueError("stream limit")
            line = raw.decode("utf-8").rstrip("\r\n")
            if not line:
                if lines:
                    data = "\n".join(lines)
                    if name == "error":
                        raise ValueError("stream error")
                    event = "[DONE]" if data == "[DONE]" else strict_json(data)
                    if isinstance(event, dict) and (event.get("error") or event.get("type") == "error"):
                        raise ValueError("stream error")
                    # A provider may echo a credential, even JSON-escaped. Redact
                    # after decoding, before logging or returning any model data.
                    safe = data if event == "[DONE]" else canonical(event)
                    safe = safe.replace(canonical(key)[1:-1], "[REDACTED]").replace(key, "[REDACTED]")
                    event = "[DONE]" if safe == "[DONE]" else strict_json(safe)
                    log.write(("event: " + name.replace(key, "[REDACTED]") + "\ndata: " + safe + "\n\n").encode())
                    yield name, event
                name, lines = "", []
            elif line.startswith("event:"):
                name = line[6:].lstrip(" ")
            elif line.startswith("data:"):
                lines.append(line[5:].lstrip(" "))

    def _parse(self, events):
        output, usage, model, finished, done = [], None, None, False, False
        for name, event in events:
            if event == "[DONE]":
                done = True
                break
            model = event.get("model", model)
            usage = event.get("usage") or usage
            for choice in event.get("choices", []):
                if choice.get("index", 0) != 0:
                    raise ValueError("multiple choices")
                delta = choice.get("delta", {})
                if delta.get("refusal"):
                    raise ValueError("refusal")
                if delta.get("content"):
                    output.append(delta["content"])
                finish = choice.get("finish_reason")
                if finish is not None:
                    if finish != "stop":
                        raise ValueError("incomplete response")
                    finished = True
        if not done or not finished or not output:
            raise ValueError("incomplete SSE")
        return "".join(output), usage, model

    def __call__(self, request, stream_path):
        # Recheck even if a caller changes the endpoint after construction.
        https_url(self.endpoint)
        started, status = time.monotonic(), 0
        try:
            key = self.key
            if not key.strip():
                raise ValueError("credential unavailable")
            req = urllib.request.Request(self.endpoint, data=canonical(self.prepare(request)).encode(),
                                         headers=self._headers(key))
            with urllib.request.build_opener(NoRedirect).open(req, timeout=STREAM_TIMEOUT) as response:
                status = response.status
                if "text/event-stream" not in response.headers.get("Content-Type", ""):
                    raise ValueError("not SSE")
                with stream_path.open("wb") as log:
                    output, usage, model = self._parse(self._events(response, log, started, key))
                # A credential can straddle multiple text deltas.
                output = output.replace(key, "[REDACTED]")
        except urllib.error.HTTPError as exc:
            status = exc.code
            exc.close()
            raise TransportError(self.endpoint_host, status) from None
        except Exception:
            # Never chain third-party exceptions, response bodies or URLs.
            raise TransportError(self.endpoint_host, status) from None
        return {"output": output, "usage": usage, "model": model, "http_status": status,
                "elapsed_sec": round(time.monotonic() - started, 3)}


class AnthropicStreamTransport(OpenAIStreamTransport):
    protocol = "anthropic"
    suffix = "/v1/messages"

    # Anthropic cannot enforce a response_format, so the instruction has to be
    # repeated in the system prompt; the fence unwrap below is only a fallback.
    SYSTEM_SUFFIX = (" Do not wrap the JSON in a markdown code fence and do not "
                     "add any text before or after it.")

    def _with_suffix(self, system):
        text = str(system)
        # prepare() is called by both the caller and __call__; appending twice
        # would make the audited request differ from the bytes actually sent.
        return text if text.endswith(self.SYSTEM_SUFFIX) else text + self.SYSTEM_SUFFIX

    def prepare(self, request):
        if "system" in request:
            return {"model": request.get("model") or self.model,
                    "max_tokens": min(4096, max(1, request.get("max_tokens", 4096))),
                    "stream": True, "system": self._with_suffix(request["system"]),
                    "messages": request["messages"]}
        return {"model": request.get("model") or self.model,
                "max_tokens": min(4096, max(1, request.get("max_completion_tokens", 4096))),
                "stream": True,
                "system": self._with_suffix(request["messages"][0]["content"]),
                "messages": [{"role": "user", "content": request["messages"][1]["content"]}]}

    def _headers(self, key):
        return {"x-api-key": key, "anthropic-version": "2023-06-01",
                "Content-Type": "application/json"}

    def _parse(self, events):
        output, model = [], None
        input_tokens, output_tokens = None, None
        started, finished, done = False, False, False
        for name, event in events:
            kind = event.get("type", name)
            if kind == "message_start":
                started = True
                message = event["message"]
                model = message.get("model")
                input_tokens = message.get("usage", {}).get("input_tokens")
            elif kind == "content_block_delta":
                delta = event["delta"]
                if delta.get("type") == "text_delta":
                    output.append(delta["text"])
            elif kind == "message_delta":
                output_tokens = event.get("usage", {}).get("output_tokens", output_tokens)
                reason = event.get("delta", {}).get("stop_reason")
                if reason is not None:
                    if reason != "end_turn":
                        raise ValueError("incomplete response")
                    finished = True
            elif kind == "message_stop":
                done = True
                break
        if not started or not finished or not done or not output:
            raise ValueError("incomplete SSE")
        usage = {"input_tokens": input_tokens, "output_tokens": output_tokens}
        if type(input_tokens) is int and type(output_tokens) is int:
            usage["total_tokens"] = input_tokens + output_tokens
        return "".join(output), usage, model



