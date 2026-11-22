"""CLI-backed agents for roles the API-only channels cannot cover.

These agents are not equivalent to the API transports: a CLI owns tools, its own
configuration and its own provider session. They run in a fresh empty working
directory with a deliberately small environment and non-interactive/read-only
flags, but this module is still a process boundary, not a security sandbox.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import time

from .core import PolicyError
from .agents_probe import find_cli, redact

DEFAULT_MODEL = "default"
TIMEOUT = 180
MAX_OUTPUT = 2 * 1024 * 1024
TAIL = 4096

# Environment keys each CLI may need to authenticate. Anything else is dropped.
ENV_ALLOW = {
    "gemini": {"GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_CLOUD_PROJECT",
               "GOOGLE_CLOUD_LOCATION", "GOOGLE_GENAI_USE_VERTEXAI"},
    "qwen": {"DASHSCOPE_API_KEY", "OPENAI_API_KEY", "OPENAI_BASE_URL",
             "OPENAI_MODEL", "QWEN_OAUTH_TOKEN"},
    "opencode": {"ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPENAI_BASE_URL",
                 "OPENROUTER_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"},
    "copilot": {"GITHUB_TOKEN", "GH_TOKEN", "COPILOT_GITHUB_TOKEN"},
    "openclaw": {"OPENAI_API_KEY", "ANTHROPIC_API_KEY", "OPENROUTER_API_KEY",
                 "GEMINI_API_KEY", "GOOGLE_API_KEY", "DASHSCOPE_API_KEY",
                 "GITHUB_TOKEN"},
}
BASE_ENV = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "SHELL", "TERM",
            "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME")

# Fixed argv construction. Flags select headless/read-only modes where offered;
# no caller-controlled flag can be injected.
AGENTS = {
    "gemini": {
        "bin": "gemini",
        "default_model": DEFAULT_MODEL,
        "argv": lambda prompt, model: [
            "gemini", "-p", prompt, "-o", "json", "--approval-mode", "plan",
            "--skip-trust",
        ] + (["-m", model] if model != DEFAULT_MODEL else []),
        "parse": "json-object",
        "docs": "https://github.com/google-gemini/gemini-cli",
    },
    "qwen": {
        "bin": "qwen",
        "default_model": DEFAULT_MODEL,
        # -p is deprecated but remains the stable one-shot form in current releases.
        "argv": lambda prompt, model: [
            "qwen", "-p", prompt, "-o", "json", "--approval-mode", "plan", "--bare",
        ] + (["-m", model] if model != DEFAULT_MODEL else []),
        "parse": "json-object",
        "docs": "https://github.com/QwenLM/qwen-code",
    },
    "opencode": {
        "bin": "opencode",
        "default_model": DEFAULT_MODEL,
        "argv": lambda prompt, model: [
            "opencode", "run", "--format", "json", "--pure", "--agent", "explore",
        ] + (["--model", model] if model != DEFAULT_MODEL else []) + [prompt],
        "parse": "jsonl",
        "docs": "https://opencode.ai/docs/",
    },
    "copilot": {
        "bin": "copilot",
        "default_model": DEFAULT_MODEL,
        # No --allow-all-* flags: tools remain denied/unapproved in headless mode.
        "argv": lambda prompt, model: [
            "copilot", "-p", prompt, "--output-format", "json", "--silent",
            "--deny-tool", "shell", "--deny-tool", "write",
            "--deny-tool", "edit", "--deny-tool", "read",
        ],
        "parse": "jsonl",
        "docs": "https://github.com/github/copilot-cli",
    },
    "openclaw": {
        "bin": "openclaw",
        "default_model": DEFAULT_MODEL,
        # infer model run is a pure one-shot model turn; it has no tools.
        "argv": lambda prompt, model: [
            "openclaw", "infer", "model", "run", "--prompt", prompt, "--json", "--local",
        ] + (["--model", model] if model != DEFAULT_MODEL else []),
        "parse": "json-object",
        "docs": "https://docs.openclaw.ai/cli/infer",
    },
}


def known(agent):
    return isinstance(agent, str) and agent in AGENTS


def default_model(agent):
    return AGENTS[agent]["default_model"]


def resolve(agent, model=None):
    if not known(agent):
        raise PolicyError("unknown CLI agent: " + str(agent))
    if model is not None and not isinstance(model, str):
        raise PolicyError("role model must be a string")
    return "cli", None, None, (model or "").strip() or default_model(agent)


def _text(value, depth=0):
    """Extract assistant text from known JSON shapes without dumping metadata."""
    if depth > 6:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(part for item in value if (part := _text(item, depth + 1)))
    if isinstance(value, dict):
        for key in ("response", "text", "content", "message", "output", "result"):
            if key in value:
                text = _text(value[key], depth + 1)
                if text:
                    return text
        for key in ("parts", "part", "choices", "data", "items"):
            if key in value:
                text = _text(value[key], depth + 1)
                if text:
                    return text
    return ""


def parse_output(agent, stdout):
    spec = AGENTS[agent]
    raw = stdout.strip()
    if not raw:
        return ""
    if spec["parse"] == "jsonl":
        chunks = []
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            chunks.append(_text(event))
        text = "\n".join(chunk for chunk in chunks if chunk).strip()
        return text or raw
    try:
        payload = json.loads(raw)
    except ValueError:
        return raw
    if isinstance(payload, dict) and payload.get("error"):
        raise PolicyError("CLI agent error: " + str(payload["error"])[:500])
    text = _text(payload).strip()
    return text or raw


def _prompt(request):
    parts = []
    for message in request.get("messages", []):
        role = message.get("role", "user")
        content = message.get("content", "")
        if isinstance(content, list):
            content = " ".join(str(item.get("text", "")) if isinstance(item, dict) else str(item)
                               for item in content)
        parts.append(f"[{role}]\n{content}")
    return "\n\n".join(parts)


class CLITransport:
    """Run a CLI as a text-only decision model in a fresh empty directory."""
    def __init__(self, agent, model=None, env=None):
        if not known(agent):
            raise PolicyError("unknown CLI agent: " + str(agent))
        self.agent = agent
        self.model = (model or "").strip() or default_model(agent)
        self.overrides = dict(env or {})
        self.binary = find_cli(AGENTS[agent]["bin"])
        if not self.binary:
            raise PolicyError("CLI agent not installed: " + AGENTS[agent]["bin"])

    def _environment(self):
        environment = {key: os.environ[key] for key in BASE_ENV if key in os.environ}
        for key in sorted(ENV_ALLOW[self.agent]):
            if key in os.environ:
                environment[key] = os.environ[key]
        # Keep children non-interactive and free of inherited model overrides.
        environment.update(self.overrides)
        environment.update({"TERM": "dumb", "NO_COLOR": "1", "CI": "1",
                            "GIT_TERMINAL_PROMPT": "0"})
        return environment

    def __call__(self, request, output_path):
        started = time.monotonic()
        prompt = _prompt(request)
        if len(prompt.encode()) > 60000:
            raise PolicyError("MODEL_INPUT_LIMIT: CLI prompt exceeds 60000 bytes")
        workspace = Path(output_path).parent / (Path(output_path).stem + "-cli")
        workspace.mkdir(parents=True, exist_ok=False)
        argv = AGENTS[self.agent]["argv"](prompt, self.model)
        # Use the resolved absolute path: the fusion service PATH may omit ~/.local/bin.
        argv[0] = self.binary
        process = None
        output = b""
        try:
            process = subprocess.Popen(
                argv, cwd=workspace, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env=self._environment(), start_new_session=True)
            try:
                stdout, stderr = process.communicate(timeout=TIMEOUT)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                stdout, stderr = process.communicate()
                raise PolicyError(f"{self.agent}: CLI timeout after {TIMEOUT}s") from None
            output = (stdout or b"")[:MAX_OUTPUT]
            if process.returncode:
                tail = redact((stderr or output)[-TAIL:].decode("utf-8", "replace"))
                raise PolicyError(f"{self.agent}: CLI exit {process.returncode}: {tail}")
            text = parse_output(self.agent, output.decode("utf-8", "replace"))
            if not text.strip():
                raise PolicyError(f"{self.agent}: CLI returned empty output")
            return {"output": text, "usage": None, "model": self.model,
                    "http_status": None,
                    "elapsed_sec": round(time.monotonic() - started, 3)}
        except PolicyError:
            raise
        except Exception as exc:
            raise PolicyError(f"{self.agent}: CLI failed: {type(exc).__name__}") from None
        finally:
            if process is not None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
            # A CLI may have written into its empty cwd; remove what it created.
            try:
                shutil.rmtree(workspace)
            except OSError:
                pass
