"""Explain whether a role agent can actually run, without spending tokens.

States are deliberately narrower than "binary exists": an installed CLI with no
usable login or API key is `login_required`, not `ready`, and a missing channel
credential is `not_configured`. No live prompt is sent here.
"""
from __future__ import annotations

import os
from pathlib import Path

from .agent_channels import CHANNELS
from .agents_probe import find_cli
from .cli_agents import AGENTS as CLI_AGENTS, ENV_ALLOW

CLI_CONFIG = {
    "gemini": ("~/.gemini",),
    "qwen": ("~/.qwen",),
    "opencode": ("~/.local/share/opencode/auth.json",),
    "openclaw": ("~/.openclaw",),
    "copilot": ("~/.copilot/config.json",),
}
CLI_LOGIN = {
    "gemini": "gemini",
    "qwen": "qwen",
    "opencode": "opencode auth login",
    "openclaw": "openclaw configure",
    "copilot": "copilot",
}


def _exists(path):
    try:
        return Path(path).expanduser().exists()
    except OSError:
        return False


def _api_health(agent):
    entry = CHANNELS.get(agent)
    if not entry:
        return None
    key_env = entry["key_env"]
    has_key = bool(os.environ.get(key_env, "").strip())
    installed = bool(find_cli(agent))
    if has_key:
        return {"state": "ready", "installed": installed, "ready": True,
                "auth_mode": "api_key", "verified": False,
                "message": "API 通道凭据已配置；真实调用时会再次校验。", "next_step": ""}
    return {"state": "not_configured", "installed": installed, "ready": False,
            "auth_mode": None, "verified": False,
            "message": f"已安装 CLI，但 API 通道缺少 {key_env}；角色执行会失败。" if installed
                       else f"API 通道缺少 {key_env}，且没有安装 {agent} CLI。",
            "next_step": f"设置环境变量 {key_env}，或安装并登录 {agent}"}


def _cli_health(agent):
    spec = CLI_AGENTS[agent]
    binary = find_cli(spec["bin"])
    if not binary:
        return {"state": "not_installed", "installed": False, "ready": False,
                "auth_mode": None, "verified": False,
                "message": f"未找到 {spec['bin']} CLI。",
                "next_step": "在设置 → 智能体接入中一键安装，或运行安装脚本"}
    has_key = any(os.environ.get(key, "").strip() for key in ENV_ALLOW[agent])
    has_login = any(_exists(path) for path in CLI_CONFIG.get(agent, ()))
    if has_key:
        return {"state": "ready", "installed": True, "ready": True,
                "auth_mode": "api_key", "verified": False,
                "message": "CLI 与 API Key 均已发现；真实调用时会再次校验。", "next_step": ""}
    if has_login:
        return {"state": "ready", "installed": True, "ready": True,
                "auth_mode": "cli_login", "verified": False,
                "message": "CLI 已安装并发现登录配置；未做真实调用。", "next_step": ""}
    return {"state": "login_required", "installed": True, "ready": False,
            "auth_mode": None, "verified": False,
            "message": "CLI 已安装，但没有 API Key 或可识别的登录配置。",
            "next_step": "在终端登录：" + CLI_LOGIN.get(agent, spec["bin"])}


def health(agent, facts=None, instance=None):
    facts = facts or {}
    if not isinstance(agent, str):
        return {"state": "error", "installed": False, "ready": False,
                "auth_mode": None, "verified": False,
                "message": "unknown agent", "next_step": ""}
    if agent.startswith("custom-"):
        record = facts.get(agent, {})
        has_key = bool(record.get("found")) or bool(record.get("env_key"))
        return {"state": "ready" if has_key else "not_configured",
                "installed": True, "ready": has_key, "auth_mode": "api_key",
                "verified": False,
                "message": "自定义模型访问已保存。" if has_key else "自定义模型缺少 Key。",
                "next_step": "" if has_key else "在设置 → 模型访问中补充 Key"}
    api = _api_health(agent)
    if api is not None and agent not in CLI_AGENTS:
        return api
    if agent in CLI_AGENTS:
        return _cli_health(agent)
    return {"state": "error", "installed": bool(facts.get(agent, {}).get("found")),
            "ready": False, "auth_mode": None, "verified": False,
            "message": "该 Agent 尚未接入调度。", "next_step": "查看智能体目录中的接入状态"}


def catalog_health(catalog, facts, instance=None):
    result = []
    for row in catalog:
        result.append({**row, "health": health(row.get("id"), facts, instance)})
    return result
