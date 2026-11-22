"""Offline channel integration; production HTTPS checks are never disabled."""
import gc
import json
import threading
import traceback
import urllib.request
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from fusion import agent_channels, bind_cli, onboarding, role_binding, transports
from fusion.agents_probe import write_private_json
from fusion.ai import DecisionClient, MODEL, RULES
from fusion.ai_schema import SCHEMAS
from fusion.core import PolicyError
from fusion.mutate import canonical
from fusion.tests.test_ai import FakeTransport, GOOD, ROUTE

KEYS = ("PENGUIN_GPT_API_KEY", "MODELFLARE_CLAUDE_API_KEY", "DEEPSEEK_API_KEY")
SECRET = 'fixture-credential-no-leak'


@pytest.fixture(autouse=True)
def keys(monkeypatch):
    for key in KEYS:
        monkeypatch.setenv(key, SECRET)


def frame(name, data):
    return ("event: " + name + "\r\ndata: " + canonical(data) + "\r\n\r\n").encode()


def stream(protocol, value):
    text = canonical(value)
    halves = (text[:len(text) // 2], text[len(text) // 2:])
    if protocol == "openai":
        return b": ping\r\n\r\n" + b"".join(frame("message", {
            "model": MODEL, "choices": [{"index": 0, "delta": {"content": part},
                                          "finish_reason": None}]}) for part in halves) + frame("message", {
            "choices": [{"delta": {}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}}) + b"data: [DONE]\r\n\r\n"
    return (frame("message_start", {"message": {"model": "claude-sonnet-5-5", "usage": {"input_tokens": 11}}})
            + b"".join(frame("content_block_delta", {"delta": {"type": "text_delta", "text": part}})
                       for part in halves)
            + frame("message_delta", {"delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 7}})
            + frame("message_stop", {}))


@contextmanager
def service(protocol, values, *, status=200, raw=None):
    received = []
    values = iter(values)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            received.append((self.path, dict(self.headers), body))
            self.send_response(status)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Location", "https://never-follow.invalid/" + SECRET)
            self.end_headers()
            payload = raw if raw is not None else stream(protocol, next(values))
            try:
                self.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_port, received
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def local_wire(monkeypatch):
    """Replace only the test opener's wire URL, keeping configured URLs HTTPS."""
    real = urllib.request.build_opener

    class LocalOpener:
        def open(self, req, timeout):
            assert timeout == transports.STREAM_TIMEOUT
            assert req.full_url.startswith("https://127.0.0.1:")
            local = urllib.request.Request(req.full_url.replace("https://", "http://", 1),
                                           data=req.data, headers=dict(req.headers), method=req.get_method())
            return real(transports.NoRedirect).open(local, timeout=timeout)

    monkeypatch.setattr(transports.urllib.request, "build_opener", lambda *args: LocalOpener())


def override(home, **channels):
    write_private_json(home / "agents.json", {"channels": channels})


def test_defaults_and_override(tmp_path):
    assert agent_channels.resolve("pi") == ("openai", "https://api.deepseek.com/v1", KEYS[2], "deepseek-flash")
    override(tmp_path, claude={"base_url": "https://private.invalid", "default_model": "custom"})
    assert agent_channels.resolve("claude", instance=tmp_path) == (
        "anthropic", "https://private.invalid", KEYS[1], "custom")
    assert agent_channels.resolve("claude", "chosen", instance=tmp_path)[3] == "chosen"
    assert agent_channels.resolve("claude")[1] == "https://modelflare.dev"


@pytest.mark.parametrize("entry", [{"extra": "x"}, {"protocol": "http"}, {"protocol": []},
    {"base_url": "http://local.invalid"}, {"base_url": "https://user:password@host"},
    {"base_url": "https://host?key=secret"}, {"base_url": "https://host:bad"},
    {"base_url": "https://host/\n"}, {"base_url": "https://"}, {"key_env": "literal-key-value"},
    {"default_model": ""}, {"default_model": 5}, []])
def test_invalid_override_refuses_load(tmp_path, entry):
    override(tmp_path, claude=entry)
    with pytest.raises(PolicyError):
        agent_channels.resolve("codex", instance=tmp_path)  # Validate every override, not just selected agent.


def test_private_file_and_duplicate_fields(tmp_path):
    override(tmp_path, claude={})
    (tmp_path / "agents.json").chmod(0o644)
    with pytest.raises(PolicyError, match="0600"):
        agent_channels.resolve("claude", instance=tmp_path)
    (tmp_path / "agents.json").chmod(0o600)
    (tmp_path / "agents.json").write_text('{"channels":{},"channels":{}}')
    with pytest.raises(PolicyError, match="cannot load"):
        agent_channels.resolve("claude", instance=tmp_path)


@pytest.mark.parametrize("agent", ["foo", "api", None, []])
def test_unknown_agent(agent):
    with pytest.raises(PolicyError, match="known agents: claude, codex, hermes, pi"):
        agent_channels.resolve(agent)


@pytest.mark.parametrize("cls", [transports.OpenAIStreamTransport, transports.AnthropicStreamTransport])
def test_transport_refuses_http(cls):
    with pytest.raises(PolicyError, match="HTTPS"):
        cls("http://127.0.0.1", KEYS[0])


def test_roles_hit_separate_services_once_and_audit(tmp_path, monkeypatch):
    local_wire(monkeypatch)
    with service("openai", [ROUTE]) as (op, openai), service("anthropic", [GOOD]) as (ap, anthropic):
        override(tmp_path, codex={"base_url": f"https://127.0.0.1:{op}/v1"},
                 claude={"base_url": f"https://127.0.0.1:{ap}"})
        client = DecisionClient(tmp_path / "round", bindings={"taizi": {"agent": "codex"},
                                "menxia": {"agent": "claude"}}, instance=tmp_path)
        assert client.decide("taizi", {}) == ROUTE
        assert client.decide("menxia", {}) == GOOD
        assert len(openai) == len(anthropic) == 1
        assert openai[0][0] == "/v1/chat/completions"
        path, headers, body = anthropic[0]
        assert path == "/v1/messages"
        assert headers["X-Api-Key"] == SECRET and headers["Anthropic-Version"] == "2023-06-01"
        assert "Authorization" not in headers
        assert body["stream"] is True and body["max_tokens"] == 4096
        assert set(body) == {"model", "max_tokens", "stream", "system", "messages"}
        assert body["model"] == "claude-sonnet-5-5"
        assert canonical(SCHEMAS["menxia"]) in body["system"]
        records = client.summary()["steps"]
        assert [(r["agent"], r["protocol"], r["endpoint_host"]) for r in records] == [
            ("codex", "openai", "127.0.0.1"), ("claude", "anthropic", "127.0.0.1")]
        assert records[0]["tokens"] == {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}
        assert records[1]["tokens"] == {"input_tokens": 11, "output_tokens": 7, "total_tokens": 18}
        assert client.summary()["total_tokens"] == 36
        assert json.loads((client.directory / records[1]["request_path"]).read_text()) == body
        assert all(SECRET not in p.read_text() for p in client.directory.iterdir() if p.is_file())


def test_anthropic_schema_retry_cache_model_and_resume(tmp_path, monkeypatch):
    local_wire(monkeypatch)
    with service("anthropic", [{}, GOOD, ROUTE, GOOD]) as (port, received):
        override(tmp_path, claude={"base_url": f"https://127.0.0.1:{port}"})
        bindings = {"menxia": {"agent": "claude"}, "taizi": {"agent": "claude", "model": "custom"}}
        client = DecisionClient(tmp_path / "round", bindings=bindings, instance=tmp_path)
        assert client.decide("menxia", {}) == GOOD
        first = json.loads(received[0][2]["messages"][0]["content"])
        second = json.loads(received[1][2]["messages"][0]["content"])
        assert first["validation_error"] is None and second["validation_error"]
        cached = client.transports["claude"]
        assert client.decide("taizi", {}) == ROUTE
        assert client.transports["claude"] is cached
        assert received[2][2]["model"] == "custom"
        assert [r["status"] for r in client.summary()["steps"]] == ["invalid", "valid", "valid"]
        del client
        gc.collect()
        resumed = DecisionClient.resume(tmp_path / "round", bindings=bindings, instance=tmp_path)
        assert resumed.decide("menxia", {}) == GOOD and resumed.calls == 4


@pytest.mark.parametrize("missing", ["", "   "])
def test_missing_key_does_not_block_setup_but_is_reported(tmp_path, monkeypatch, capsys, missing):
    """A fresh machine has no keys yet; setup must still complete.

    The credential is reported as a warning at bind time and enforced when a
    run actually needs the channel, so first-run onboarding is never a dead end
    and a missing key can never pass silently either.
    """
    monkeypatch.setenv(KEYS[1], missing)
    facts = {"claude": {"found": True}}
    monkeypatch.setattr(bind_cli, "probe", lambda *a, **kw: facts)
    monkeypatch.setattr(onboarding, "probe", lambda *a, **kw: facts)
    binding = {"mode": "single", "bindings": {r: {"agent": "claude", "model": "m"} for r in role_binding.ROLES}}

    saved = role_binding.save(tmp_path, binding, facts)
    assert (tmp_path / "roles.json").exists()
    assert any(KEYS[1] in w for w in saved.get("warnings", [])), saved

    (tmp_path / "roles.json").unlink()
    assert bind_cli.main(["bind", "--home", str(tmp_path), "--mode", "single", "--agent", "claude:m"]) == 0
    assert KEYS[1] in capsys.readouterr().out
    assert (tmp_path / "roles.json").exists()

    for path, body in (("single", {"agent": "claude"}), ("multi", {})):
        code, value = onboarding.handle(tmp_path, "POST", "/api/onboarding/" + path, canonical(body).encode())
        assert code == 200, (path, value)
        assert any(KEYS[1] in w for w in value.get("warnings", [])), (path, value)


def test_run_fails_with_named_credential_when_key_missing(tmp_path, monkeypatch):
    """The warning above is only safe if a real run refuses loudly."""
    monkeypatch.setenv(KEYS[1], "")
    from fusion import ai
    client = ai.DecisionClient(tmp_path / "round",
                               bindings={"taizi": {"agent": "claude"}}, instance=tmp_path)
    with pytest.raises(PolicyError, match=KEYS[1]):
        client.decide("taizi", {"request": "x"})


def test_unknown_binding_lists_known_agents(tmp_path):
    binding = role_binding.default()
    binding["bindings"]["taizi"]["agent"] = "foo"
    with pytest.raises(PolicyError, match="known agents: claude, codex, hermes, pi"):
        role_binding.save(tmp_path, binding, {"foo": {"found": True}})


@pytest.mark.parametrize("protocol,cls", [("openai", transports.OpenAIStreamTransport),
                                          ("anthropic", transports.AnthropicStreamTransport)])
@pytest.mark.parametrize("status", [302, 401, 503])
def test_errors_never_leak_or_redirect(tmp_path, monkeypatch, protocol, cls, status):
    local_wire(monkeypatch)
    with service(protocol, [], status=status, raw=SECRET.encode()) as (port, received):
        agent = "codex" if protocol == "openai" else "claude"
        override(tmp_path, **{agent: {"base_url": f"https://127.0.0.1:{port}"}})
        client = DecisionClient(tmp_path / "round", bindings={"taizi": {"agent": agent}}, instance=tmp_path)
        with pytest.raises(PolicyError) as exc:
            client.decide("taizi", {})
        assert str(exc.value) == f"taizi: {agent}/{protocol} 127.0.0.1 HTTP {status}"
        assert SECRET not in "".join(traceback.format_exception(exc.value))
        assert len(received) == 1 and client.calls == 1
        assert client.summary()["steps"][0]["http_status"] == status
        assert all(SECRET not in p.read_text() for p in client.directory.iterdir() if p.is_file())


@pytest.mark.parametrize("protocol", ["openai", "anthropic"])
@pytest.mark.parametrize("damage", ["truncated", "event_error", "json_error", "size", "time"])
def test_stream_fail_closed(tmp_path, monkeypatch, protocol, damage):
    local_wire(monkeypatch)
    data = stream(protocol, ROUTE)
    if damage == "truncated":
        data = data.rsplit(b"\r\n\r\n", 2)[0] + b"\r\n\r\n"
    elif damage == "event_error":
        data = frame("error", {"message": SECRET})
    elif damage == "json_error":
        data = b"data: {" + SECRET.encode() + b"}\n\n"
    elif damage == "size":
        monkeypatch.setattr(transports, "MAX_STREAM_BYTES", 30)
    else:
        monkeypatch.setattr(transports, "MAX_STREAM_SECONDS", -1)
    with service(protocol, [], raw=data) as (port, _):
        cls = transports.OpenAIStreamTransport if protocol == "openai" else transports.AnthropicStreamTransport
        transport = cls(f"https://127.0.0.1:{port}", KEYS[0])
        with pytest.raises(transports.TransportError) as exc:
            transport({"model": MODEL, "messages": [{"content": "rules"}, {"content": "{}"}]}, tmp_path / "stream")
        assert str(exc.value) == "127.0.0.1 HTTP 200"
        assert SECRET not in "".join(traceback.format_exception(exc.value))


def test_unbound_request_and_audit_are_unchanged(tmp_path):
    transport = FakeTransport([ROUTE])
    client = DecisionClient(tmp_path / "round", transport)
    assert client.decide("taizi", {}) == ROUTE
    expected = {"model": MODEL, "stream": True, "reasoning_effort": "low", "max_completion_tokens": 4096,
                "stream_options": {"include_usage": True}, "messages": [
                    {"role": "system", "content": RULES["taizi"] + " Return ONLY a JSON object matching this schema: " + canonical(SCHEMAS["taizi"])},
                    {"role": "user", "content": canonical({"input": {}, "validation_error": None})}],
                "response_format": {"type": "json_schema", "json_schema": {"name": "taizi", "strict": True, "schema": SCHEMAS["taizi"]}}}
    assert (client.directory / "01-taizi.request.json").read_bytes() == canonical(expected).encode()
    assert transport.requests == [expected]
    assert not ({"agent", "protocol", "endpoint_host"} & set(client.summary()["steps"][0]))


def test_unbound_role_does_not_poison_bound_routing(tmp_path, monkeypatch):
    from fusion import ai
    local_wire(monkeypatch)
    legacy = FakeTransport([ROUTE])
    monkeypatch.setattr(ai, "StreamTransport", lambda: legacy)
    with service("anthropic", [GOOD]) as (port, received):
        override(tmp_path, claude={"base_url": f"https://127.0.0.1:{port}"})
        client = DecisionClient(tmp_path / "round", bindings={"menxia": {"agent": "claude"}}, instance=tmp_path)
        assert client.decide("taizi", {}) == ROUTE
        assert client.decide("menxia", {}) == GOOD
        assert len(legacy.requests) == len(received) == 1


@pytest.mark.parametrize("protocol", ["openai", "anthropic"])
def test_echoed_credential_is_redacted_across_deltas(tmp_path, monkeypatch, protocol):
    local_wire(monkeypatch)
    # Place a credential across the fake server's text chunk boundary.
    value = {**ROUTE, "title": SECRET * 8}
    agent = "codex" if protocol == "openai" else "claude"
    with service(protocol, [value]) as (port, _):
        override(tmp_path, **{agent: {"base_url": f"https://127.0.0.1:{port}"}})
        client = DecisionClient(tmp_path / "round", bindings={"taizi": {"agent": agent}}, instance=tmp_path)
        result = client.decide("taizi", {})
        assert SECRET not in result["title"]
        assert all(SECRET not in p.read_text() for p in client.directory.iterdir() if p.is_file())


def test_anthropic_token_limit():
    transport = transports.AnthropicStreamTransport("https://never-called.invalid", KEYS[1])
    for limit in (1, 4096, 9000):
        body = transport.prepare({"model": "m", "max_completion_tokens": limit,
                                  "messages": [{"content": "rules"}, {"content": "{}"}]})
        assert body["max_tokens"] == min(4096, limit)
        assert "response_format" not in body


@pytest.mark.parametrize("bound", [False, True])
def test_web_worker_reads_instance_binding(tmp_path, monkeypatch, bound):
    from types import SimpleNamespace
    from fusion import ai, server
    captured = []
    bindings = {"menxia": {"agent": "claude", "model": "claude-sonnet-5-5"}}
    if bound:
        write_private_json(tmp_path / "roles.json", {"mode": "multi", "bindings": bindings})
    monkeypatch.setattr(ai, "DecisionClient", lambda *args, **kw: captured.append((args, kw)))
    httpd = SimpleNamespace(store=SimpleNamespace(home=tmp_path), stop_worker=False)

    def consume(factory, **kwargs):
        factory(tmp_path / "audit")
        httpd.stop_worker = True
        return {"done": True}

    httpd.api = SimpleNamespace(process_next=consume)
    server.worker_loop(httpd)
    assert captured == [((tmp_path / "audit",), {"bindings": bindings, "instance": tmp_path} if bound else {})]


def test_admin_preset_api_reports_missing_key_without_blocking(tmp_path, monkeypatch):
    from fusion import agent_install
    monkeypatch.setenv(KEYS[1], "")
    monkeypatch.setattr(agent_install, "probe", lambda *a, **kw: {"claude": {"found": True}})
    installer = agent_install.Installer(tmp_path)
    code, result = installer.handle("POST", "/api/agents/bind", b"{}", {"name": "boss", "role": "admin"})
    assert code == 200, result
    assert any(KEYS[1] in w for w in result.get("warnings", [])), result
    assert (tmp_path / "roles.json").exists()
