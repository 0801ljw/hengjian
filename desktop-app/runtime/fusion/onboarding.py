"""First-run preferences, reusing the discovered facts and board preset."""
from __future__ import annotations

import ipaddress
import json
from pathlib import Path
from urllib.parse import urlsplit

from .agent_catalog import catalog
from .agent_health import catalog_health, health
from .agent_registry import load as load_registry
from .agent_install import installability
from .agents_probe import probe, write_private_json
from .agent_channels import resolve
from .core import now
from .role_binding import ROLES, apply_preset, load, preset_status, recommendations, save

# 一个 agent 同时承担门下审查和工部执行，综合推理与输出质量优先于成本和速度。
# 签批处依据：claude 推理/审查口碑最强、1M 上下文；codex 本项目实测
# 结构化输出最稳、跨入口；hermes 调用量大但有 E2BIG/provider 不识别记录；
# pi 便宜快、适合分类排序，不宜独占全部角色。仅影响默认高亮，允许自由改选。
CAPABILITY_ORDER = ("claude", "codex", "hermes", "pi")
PATHS = {"/api/onboarding", "/api/onboarding/single", "/api/onboarding/multi",
         "/api/onboarding/complete"}


def required(instance):
    return not (Path(instance) / "roles.json").exists()


def _loopback(host):
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host == "localhost"


def allowed(users, session, peer, host, origin=None):
    """Fail closed; anonymous bootstrap is local and same-origin only.

    Host checking also prevents a remote website using DNS rebinding against the
    no-user window. Forwarded headers never establish a local connection.
    """
    if users.users():
        return bool(session and session.get("role") == "admin")
    try:
        authority = urlsplit("http://" + host)
        if not _loopback(peer) or not _loopback(authority.hostname or ""):
            return False
        if origin:
            source = urlsplit(origin)
            if source.scheme not in ("http", "https") or source.netloc != host:
                return False
        return True
    except ValueError:
        return False


def _registry_payload(instance):
    agents, source = load_registry(instance)
    return {"agents": agents, "source": source}


def status(instance):
    facts = probe(instance, refresh=True)
    return {"required": required(instance),
            "agents": [{"agent": name, **entry} for name, entry in facts.items()],
            "capability_order": list(CAPABILITY_ORDER),
            "suggested": next((name for name in CAPABILITY_ORDER
                               if facts.get(name, {}).get("found")), None),
            "preset": preset_status(facts), "installable": installability(),
            "catalog": catalog_health(catalog(facts, installability(), instance), facts, instance),
            "health": {name: health(name, facts, instance) for name in facts},
            "registry": _registry_payload(instance),
            "recommendations": recommendations()}


def complete(instance):
    if required(instance):
        raise ValueError("请先选择单个或多个 Agent 并保存绑定")
    path = Path(instance) / "onboarding.json"
    mode = load(instance)["mode"]
    try:
        previous = json.loads(path.read_text())
    except FileNotFoundError:
        previous = {}
    if previous.get("mode") == mode and previous.get("completed_at"):
        return previous
    result = {"completed_at": now(), "mode": mode}
    write_private_json(path, result)
    return result


def handle(instance, method, path, body):
    if path == "/api/onboarding" and method == "GET":
        return 200, status(instance)
    if method != "POST" or path == "/api/onboarding":
        return 405, {"error": "method not allowed"}
    try:
        data = json.loads(body or b"{}")
        if not isinstance(data, dict):
            raise ValueError("payload must be an object")
        if path == "/api/onboarding/single":
            facts = probe(instance)
            agent = data.get("agent")
            if set(data) != {"agent"}:
                raise ValueError("payload requires only agent")
            channel = resolve(agent, instance=instance)
            if not facts.get(agent, {}).get("found"):
                raise ValueError("请选择已探测到的 agent")
            # Preserve an actual configured model when available; otherwise use
            # the channel's explicit default model.
            model = facts[agent].get("model")
            if not isinstance(model, str) or not model.strip():
                model = channel[3]
            value = save(instance, {"mode": "single", "bindings": {
                role: {"agent": agent, "model": model} for role in ROLES}}, facts)
            # Reset the receipt when a new selection is saved.
            write_private_json(Path(instance) / "onboarding.json", {"mode": "single"})
            return 200, {**value, **complete(instance), "missing": []}
        if data:
            raise ValueError("payload must be empty")
        if path == "/api/onboarding/multi":
            value = apply_preset(probe(instance), instance)
            write_private_json(Path(instance) / "onboarding.json", {"mode": "multi"})
            return 200, value
        if path == "/api/onboarding/complete":
            return 200, complete(instance)
    except (ValueError, UnicodeError) as exc:
        return 400, {"error": str(exc)}
    return 404, {"error": "not found"}
