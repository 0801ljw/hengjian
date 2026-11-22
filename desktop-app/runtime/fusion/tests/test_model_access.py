"""Custom model access records are local and never expose keys through public()."""
import os

from fusion.agent_channels import resolve
from fusion.model_access import ModelAccess
from fusion.core import PolicyError


def test_custom_model_crud_and_resolution(tmp_path):
    access = ModelAccess(tmp_path)
    record = access.save({
        "name": "My Relay",
        "protocol": "openai",
        "base_url": "https://relay.example.com/v1",
        "model": "my-model",
        "api_key": "sk-local-fixture",
    })
    assert record["id"].startswith("custom-my-relay-")
    assert "api_key" not in access.public()[0]
    key_env = "DUTANG_MODEL_KEY_" + record["id"].upper().replace("-", "_")
    assert os.environ[key_env] == "sk-local-fixture"
    assert access.channels()[record["id"]]["default_model"] == "my-model"
    assert resolve(record["id"], instance=tmp_path) == (
        "openai", "https://relay.example.com/v1", key_env, "my-model")
    access.delete(record["id"])
    assert access.public() == []
    assert not os.environ.get(key_env)


def test_custom_model_rejects_public_http_and_bad_protocol(tmp_path):
    access = ModelAccess(tmp_path)
    for payload in (
        {"name": "x", "protocol": "openai", "base_url": "http://example.com/v1",
         "model": "m", "api_key": "k"},
        {"name": "x", "protocol": "grpc", "base_url": "https://example.com/v1",
         "model": "m", "api_key": "k"},
    ):
        try:
            access.save(payload)
        except PolicyError:
            continue
        raise AssertionError("invalid model access was accepted")


def test_model_test_checks_loopback_http_endpoint(tmp_path):
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            ok = self.path == "/v1/models" and self.headers.get("Authorization") == "Bearer sk-test"
            self.send_response(200 if ok else 401)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"data":[{"id":"m"}]}')

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        access = ModelAccess(tmp_path)
        result = access.test({"provider": "custom", "api_key": "sk-test",
                              "base_url": f"http://127.0.0.1:{server.server_port}/v1",
                              "model": "m"})
        assert result["ok"] is True
        assert result["models"] == [{"id": "m", "label": "m"}]
    finally:
        server.shutdown()


def test_provider_presets_and_model_list_metadata(tmp_path):
    from fusion.model_providers import providers, get
    assert get("openai")["base_url"].startswith("https://")
    data = providers()
    assert any(provider["id"] == "custom-anthropic" for provider in data["providers"])
    access = ModelAccess(tmp_path)
    record = access.save({"provider": "openai", "name": "OpenAI",
                          "model": "gpt-test", "api_key": "sk-test",
                          "models": [{"id": "gpt-test", "label": "GPT Test"}]})
    assert record["provider"] == "openai"
    assert access.public()[0]["models"] == [{"id": "gpt-test", "label": "GPT Test"}]
    assert access.channels()[record["id"]]["base_url"] == "https://api.openai.com/v1"
