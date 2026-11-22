"""Launcher-side cloud workspace link (stored under the local instance)."""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

from .core import PolicyError, now


def _path(instance):
    path = Path(instance) / "launcher.json"
    return path


def _load(instance):
    try:
        data = json.loads(_path(instance).read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(instance, data):
    path = _path(instance)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    tmp.chmod(0o600)
    tmp.replace(path)


def _request(method, url, body=None, token=None, timeout=10):
    headers = {"Content-Type": "application/json",
               "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                             "AppleWebKit/537.36 (KHTML, like Gecko) "
                             "Chrome/131.0 Safari/537.36 HengjianLauncher/0.1"}
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read() or b"{}")
        except ValueError:
            payload = {}
        raise PolicyError(payload.get("error") or f"cloud HTTP {exc.code}") from None
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        raise PolicyError(f"cannot reach cloud workspace: {exc}") from None


def status(instance):
    data = _load(instance)
    return {"paired": bool(data.get("id") and data.get("token")),
            "cloud_url": data.get("cloud_url"),
            "launcher_id": data.get("id"),
            "name": data.get("name"),
            "paired_at": data.get("paired_at"),
            "last_heartbeat": data.get("last_heartbeat")}


def pair(instance, cloud_url, code, name="Hengjian Launcher", host="", version=""):
    cloud_url = str(cloud_url or "").strip().rstrip("/")
    if not cloud_url.startswith(("http://", "https://")):
        raise PolicyError("cloud_url must be http(s)")
    if not str(code or "").strip():
        raise PolicyError("pairing code required")
    _, data = _request("POST", cloud_url + "/api/launchers/claim",
                       {"code": str(code).strip().upper(), "name": name,
                        "host": host, "version": version})
    if not data.get("launcher_id") or not data.get("token"):
        raise PolicyError("cloud returned an invalid launcher")
    record = {"cloud_url": cloud_url, "id": data["launcher_id"], "token": data["token"],
              "name": name, "host": host, "version": version,
              "paired_at": now(), "last_heartbeat": None}
    _save(instance, record)
    return status(instance)


def disconnect(instance):
    path = _path(instance)
    path.unlink(missing_ok=True)
    return status(instance)


def heartbeat(instance):
    data = _load(instance)
    if not data.get("id") or not data.get("token"):
        return False
    try:
        _request("POST", data["cloud_url"] + f"/api/launchers/{data['id']}/heartbeat",
                 {}, token=data["token"])
    except PolicyError:
        return False
    data["last_heartbeat"] = now()
    _save(instance, data)
    return True
