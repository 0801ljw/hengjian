"""User-defined model access records (OpenAI/Anthropic-compatible endpoints).

Keys are stored in a 0600 sidecar under the instance, never returned by the API.
This is a local convenience store, not a KMS; deployments that need stronger
secret custody should keep their existing external secret manager.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import secrets
import stat
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from .agents_probe import write_private_json
from . import model_providers
from .core import PolicyError, now

MODELS = "models.json"
KEYS = "model-keys.json"
KEY_PREFIX = "DUTANG_MODEL_KEY_"
PROTOCOLS = {"openai", "anthropic"}
MAX_MODELS = 32


def _private_json(path):
    path = Path(path)
    try:
        if stat.S_IMODE(path.stat().st_mode) != 0o600:
            raise PolicyError(f"{path.name} must have mode 0600")
        return json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except PolicyError:
        raise
    except (OSError, ValueError):
        raise PolicyError(f"cannot read {path.name}") from None


def _url(value, *, allow_loopback_http=True):
    if not isinstance(value, str) or any(c.isspace() or ord(c) < 32 for c in value):
        raise PolicyError("接口地址格式无效")
    try:
        parsed = urlsplit(value)
        if parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
            raise ValueError
        if parsed.scheme == "https" and parsed.hostname:
            return value.rstrip("/")
        loopback = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        if allow_loopback_http and parsed.scheme == "http" and loopback:
            return value.rstrip("/")
    except ValueError:
        pass
    raise PolicyError("接口地址必须是 HTTPS，或本机回环的 HTTP")


def _key_env(identifier):
    return KEY_PREFIX + re.sub(r"[^A-Z0-9]", "_", identifier.upper())


def _identifier(name):
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:36]
    if not slug:
        slug = "model"
    return "custom-" + slug + "-" + secrets.token_hex(3)


def _clean(record):
    return {key: record.get(key) for key in ("id", "name", "provider", "protocol", "base_url",
                                             "model", "models", "created_at")}


def _validate_payload(payload, *, require_key=False):
    if not isinstance(payload, dict):
        raise PolicyError("payload must be an object")
    allowed = {"name", "provider", "protocol", "base_url", "model", "api_key", "models"}
    if set(payload) - allowed:
        raise PolicyError("unknown model fields")
    name = payload.get("name")
    provider = payload.get("provider") or "custom"
    protocol = payload.get("protocol")
    base_url = payload.get("base_url")
    model = payload.get("model")
    api_key = payload.get("api_key")
    models = payload.get("models") or []
    if not isinstance(name, str) or not name.strip() or len(name) > 60:
        raise PolicyError("名称必须为 1..60 个字符")
    preset = model_providers.get(provider)
    if preset is None:
        raise PolicyError("unknown provider")
    if protocol is None:
        protocol = preset["protocol"]
    if protocol not in PROTOCOLS:
        raise PolicyError("protocol must be openai or anthropic")
    if base_url is None or not str(base_url).strip():
        base_url = preset["base_url"]
    if not isinstance(model, str) or not model.strip() or len(model) > 200:
        raise PolicyError("模型名必须为 1..200 个字符")
    base_url = _url(base_url)
    if not isinstance(models, list) or any(not isinstance(item, dict) or not isinstance(item.get("id"), str)
                                           for item in models):
        raise PolicyError("models must be a list of {id,label}")
    models = [{"id": item["id"][:200], "label": str(item.get("label") or item["id"])[:80]}
              for item in models[:300]]
    if api_key is not None and (not isinstance(api_key, str) or not api_key.strip()):
        raise PolicyError("API Key 不能为空")
    if require_key and not api_key:
        raise PolicyError("新建模型需要 API Key")
    return {"name": name.strip(), "provider": provider, "protocol": protocol,
            "base_url": base_url, "model": model.strip(), "api_key": api_key,
            "models": models}


class ModelAccess:
    def __init__(self, instance):
        self.instance = Path(instance)

    def _paths(self):
        return self.instance / MODELS, self.instance / KEYS

    def _load_keys(self):
        keys = _private_json(self._paths()[1]) or {}
        if not isinstance(keys, dict):
            raise PolicyError("model-keys.json must be an object")
        return {key: value for key, value in keys.items() if isinstance(value, str)}

    def records(self):
        data = _private_json(self._paths()[0])
        if data is None:
            return []
        if not isinstance(data, dict) or set(data) - {"models"} or not isinstance(data.get("models"), list):
            raise PolicyError("models.json is invalid")
        rows = []
        for raw in data["models"]:
            if not isinstance(raw, dict):
                raise PolicyError("models.json is invalid")
            try:
                row = _validate_payload({key: raw[key] for key in
                                         ("name", "provider", "protocol", "base_url", "model", "models")
                                         if key in raw})
            except PolicyError:
                continue
            pid = raw.get("id")
            if not isinstance(pid, str) or not re.fullmatch(r"custom-[a-z0-9-]{1,60}", pid):
                continue
            rows.append({**row, "id": pid, "created_at": raw.get("created_at") or now(),
                         "key_env": _key_env(pid)})
        return rows

    def public(self):
        return [_clean(row) for row in self.records()]

    def _install_env(self, rows):
        keys = self._load_keys()
        for row in rows:
            key = keys.get(row["id"])
            if key:
                os.environ[row["key_env"]] = key

    def channels(self):
        rows = self.records()
        self._install_env(rows)
        return {row["id"]: {"protocol": row["protocol"], "base_url": row["base_url"],
                            "key_env": row["key_env"], "default_model": row["model"] or "default"}
                for row in rows}

    def save(self, payload):
        values = _validate_payload(payload, require_key=True)
        models_path, keys_path = self._paths()
        rows = self.records()
        if len(rows) >= MAX_MODELS:
            raise PolicyError("最多保存 32 个模型访问")
        identifier = _identifier(values["name"])
        row = {"id": identifier, "name": values["name"], "provider": values["provider"],
               "protocol": values["protocol"], "base_url": values["base_url"],
               "model": values["model"], "models": values["models"], "created_at": now()}
        keys = self._load_keys()
        keys[identifier] = values["api_key"].strip()
        write_private_json(models_path, {"models": [*[{key: item[key] for key in
            ("id", "name", "provider", "protocol", "base_url", "model", "models", "created_at")
            if key in item} for item in rows], row]})
        write_private_json(keys_path, keys)
        os.chmod(keys_path, 0o600)
        os.environ[_key_env(identifier)] = keys[identifier]
        return _clean(row)

    def delete(self, identifier):
        if not isinstance(identifier, str) or not re.fullmatch(r"custom-[a-z0-9-]{1,60}", identifier):
            raise PolicyError("unknown model")
        models_path, keys_path = self._paths()
        rows = self.records()
        if not any(row["id"] == identifier for row in rows):
            raise PolicyError("unknown model")
        write_private_json(models_path, {"models": [{
            key: row[key] for key in ("id", "name", "provider", "protocol", "base_url", "model",
                                      "models", "created_at") if key in row}
            for row in rows if row["id"] != identifier]})
        keys = self._load_keys()
        keys.pop(identifier, None)
        write_private_json(keys_path, keys)
        os.chmod(keys_path, 0o600)
        os.environ.pop(_key_env(identifier), None)
        return {"deleted": identifier}

    def _probe_payload(self, payload):
        if not isinstance(payload, dict):
            raise PolicyError("payload must be an object")
        allowed = {"provider", "api_key", "base_url", "model", "name", "protocol", "models"}
        if set(payload) - allowed:
            raise PolicyError("unknown probe fields")
        provider = payload.get("provider")
        preset = model_providers.get(provider)
        if preset is None:
            raise PolicyError("unknown provider")
        api_key = payload.get("api_key")
        if not isinstance(api_key, str) or not api_key.strip():
            raise PolicyError("API Key 不能为空")
        base = payload.get("base_url") or preset["base_url"]
        if not base:
            raise PolicyError("该提供商需要接口地址")
        base = _url(base)
        return provider, preset, base.rstrip("/"), api_key.strip(), payload.get("model")

    @staticmethod
    def _parse_models(text):
        try:
            parsed = json.loads(text)
        except ValueError:
            return []
        entries = parsed if isinstance(parsed, list) else (
            parsed.get("data") or parsed.get("models") or [] if isinstance(parsed, dict) else [])
        if not isinstance(entries, list):
            return []
        result = []
        seen = set()
        for item in entries:
            if isinstance(item, str):
                identifier = item.strip()
                label = identifier
            elif isinstance(item, dict):
                identifier = str(item.get("id") or "").strip()
                label = str(item.get("display_name") or item.get("name") or identifier).strip()
            else:
                continue
            if not identifier or identifier in seen:
                continue
            seen.add(identifier)
            result.append({"id": identifier[:200], "label": label[:80] or identifier[:200]})
        result.sort(key=lambda item: item["id"].lower())
        return result[:300]

    def test(self, payload):
        provider, preset, base, api_key, model = self._probe_payload(payload)
        if preset["protocol"] == "anthropic":
            url = base + ("/v1/models" if not base.endswith("/v1") else "/models")
            if urlsplit(base).hostname == "api.anthropic.com":
                headers = {"x-api-key": api_key}
            else:
                headers = {"Authorization": "Bearer " + api_key}
            headers["anthropic-version"] = "2023-06-01"
        else:
            path = urlsplit(base).path.rstrip("/")
            # A path already names the API root (…/v1, …/v1beta/openai);
            # only a bare host needs /v1 added.
            url = base + ("/models" if path else "/v1/models")
            headers = {"Authorization": "Bearer " + api_key}
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        try:
            request = urllib.request.Request(url, headers=headers, method="GET")
            with urllib.request.build_opener(NoRedirect).open(request, timeout=15) as response:
                body = response.read(2_000_000).decode("utf-8", "replace")
                models = self._parse_models(body)
                if not models:
                    return {"ok": False, "keyOk": None, "status": response.status,
                            "models": [], "error": "该地址未提供模型列表，可先保存"}
                return {"ok": True, "keyOk": True, "status": response.status, "models": models}
        except urllib.error.HTTPError as exc:
            try:
                detail = exc.read(2000).decode("utf-8", "replace")
                parsed = json.loads(detail)
                message = parsed.get("error") if isinstance(parsed, dict) else None
                if isinstance(message, dict):
                    message = message.get("message")
            except (ValueError, OSError):
                message = None
            return {"ok": False, "keyOk": False, "status": exc.code,
                    "error": str(message or f"HTTP {exc.code}")[:160]}
        except (OSError, ValueError) as exc:
            return {"ok": False, "keyOk": False, "status": 0, "error": type(exc).__name__}

    def handle(self, method, path, body, session):
        # Provider metadata is public; saved records and keys stay admin-only.
        if path == "/api/models/providers" and method == "GET":
            return 200, model_providers.providers()
        if not session or session.get("role") != "admin":
            return 401 if not session else 403, {"error": "admin session required"}
        try:
            if path == "/api/models" and method == "GET":
                return 200, {"models": self.public(), **model_providers.providers()}
            if path == "/api/models/test" and method == "POST":
                data = json.loads(body or b"{}")
                return 200, self.test(data)
            if path == "/api/models" and method == "POST":
                data = json.loads(body or b"{}")
                return 200, self.save(data)
            if path.startswith("/api/models/") and method == "DELETE":
                return 200, self.delete(path.removeprefix("/api/models/"))
            return 405, {"error": "method not allowed"}
        except (PolicyError, ValueError, KeyError) as exc:
            return 400, {"error": str(exc)}
