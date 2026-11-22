"""Display catalog for local agent onboarding.

Facts come from agents_probe; installability comes from agent_install. This module
only describes what the UI may show and never grants execution authority.
"""
from __future__ import annotations

HERMES_DOCS = "https://hermes-agent.nousresearch.com/docs/getting-started/installation/"

# integrated: the fusion scheduler can bind this agent to a role today.
# installable: agent_install has a fixed, one-click npm recipe.
CATALOG = (
    {
        "id": "codex",
        "title": "OpenAI Codex CLI",
        "vendor": "OpenAI",
        "description": "终端编码 agent；结构化输出稳定，默认承担中书与工部。",
        "roles": ("中书", "工部"),
        "integrated": True,
        "installable": True,
        "docs": "https://github.com/openai/codex",
    },
    {
        "id": "claude",
        "title": "Claude Code",
        "vendor": "Anthropic",
        "description": "推理与审查能力强；默认承担门下封驳。",
        "roles": ("门下",),
        "integrated": True,
        "installable": True,
        "docs": "https://docs.anthropic.com/en/docs/claude-code",
    },
    {
        "id": "pi",
        "title": "Pi",
        "vendor": "Earendil Works",
        "description": "轻量、低成本的编码 agent；适合太子分拣与尚书派发。",
        "roles": ("太子", "尚书"),
        "integrated": True,
        "installable": True,
        "docs": "https://github.com/earendil-works/pi",
    },
    {
        "id": "hermes",
        "title": "Hermes Agent",
        "vendor": "Nous Research",
        "description": "带工具、记忆与 profile 的自我改进 agent；适合高频判定。",
        "roles": ("太子",),
        "integrated": True,
        "installable": False,
        "docs": HERMES_DOCS,
        "manual_reason": "需按官方文档手动安装",
    },
    {
        "id": "gemini",
        "title": "Gemini CLI",
        "vendor": "Google",
        "description": "已接入 CLI 调度；可在角色绑定中选用。",
        "roles": (),
        "integrated": True,
        "installable": True,
        "docs": "https://github.com/google-gemini/gemini-cli",
    },
    {
        "id": "aider",
        "title": "Aider",
        "vendor": "Aider",
        "description": "已接入 CLI 调度；可在角色绑定中选用。",
        "roles": (),
        "integrated": False,
        "installable": True,
        "docs": "https://aider.chat/docs/install.html",
    },
    {
        "id": "cursor-agent",
        "title": "Cursor Agent",
        "vendor": "Anysphere",
        "description": "已接入 CLI 调度；可在角色绑定中选用。",
        "roles": (),
        "integrated": False,
        "installable": False,
        "docs": "https://docs.cursor.com/en/cli/overview",
        "manual_reason": "官方脚本安装，需手动执行",
    },
    {
        "id": "opencode",
        "title": "OpenCode",
        "vendor": "OpenCode",
        "description": "已接入 CLI 调度；可在角色绑定中选用。",
        "roles": (),
        "integrated": True,
        "installable": True,
        "docs": "https://opencode.ai/docs/",
    },
    {
        "id": "openclaw",
        "title": "OpenClaw",
        "vendor": "Open source",
        "description": "多通道 AI gateway；通过纯推理 CLI 接入调度。",
        "roles": (),
        "integrated": True,
        "installable": True,
        "docs": "https://openclaw.ai/",
    },
    {
        "id": "qwen",
        "title": "Qwen Code",
        "vendor": "Alibaba",
        "description": "Qwen 终端编码 agent；已接入 CLI 调度。",
        "roles": (),
        "integrated": True,
        "installable": True,
        "docs": "https://github.com/QwenLM/qwen-code",
    },
    {
        "id": "copilot",
        "title": "GitHub Copilot CLI",
        "vendor": "GitHub",
        "description": "终端 Copilot agent；已接入 CLI 调度。",
        "roles": (),
        "integrated": True,
        "installable": True,
        "docs": "https://github.com/github/copilot-cli",
    },
)


def catalog(facts, installability, instance=None):
    """Return UI rows; detection is local-only and never claims capability."""
    result = []
    for entry in CATALOG:
        fact = facts.get(entry["id"], {})
        install = installability.get(entry["id"], {})
        result.append({
            **entry,
            "found": bool(fact.get("found")),
            "version": fact.get("version"),
            "path": fact.get("path"),
            "installable": bool(entry["installable"] and install.get("installable")),
            "reason": install.get("reason") or entry.get("manual_reason"),
            "docs": install.get("docs") or entry.get("docs"),
        })
    if instance is not None:
        try:
            from .agent_registry import load as load_registry
            registry_rows, _ = load_registry(instance)
            metadata = {row.get("id"): row for row in registry_rows if isinstance(row, dict)}
            for item in result:
                remote = metadata.get(item["id"])
                if remote:
                    for key in ("title", "vendor", "description", "docs"):
                        if remote.get(key):
                            item[key] = remote[key]
        except Exception:
            pass
        try:
            from .agent_registry import load as load_registry
            known = {item["id"] for item in result}
            registry_rows, _ = load_registry(instance)
            for row in registry_rows:
                if not isinstance(row, dict) or row.get("id") in known:
                    continue
                name = row.get("id")
                fact = facts.get(name, {})
                result.append({
                    "id": name, "title": row.get("title") or name,
                    "vendor": row.get("vendor") or "", "description": row.get("description") or "",
                    "roles": (), "integrated": False, "installable": False,
                    "found": bool(fact.get("found")), "version": fact.get("version"),
                    "path": fact.get("path"), "docs": row.get("docs") or None,
                })
        except Exception:
            pass
        try:
            from .model_access import ModelAccess
            for row in ModelAccess(instance).records():
                result.append({
                    "id": row["id"], "title": row["name"], "vendor": "自定义",
                    "description": f"自定义 {row['protocol']} 兼容模型访问。",
                    "roles": (), "integrated": True, "installable": False,
                    "found": True, "version": row["model"], "path": None,
                    "docs": None, "reason": "自定义模型访问",
                })
        except Exception:
            pass
    return result
