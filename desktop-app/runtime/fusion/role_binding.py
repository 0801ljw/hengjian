"""Explicit role preferences; never grant execution or provider authority."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

from .ai import MODEL
from .agents_probe import probe, write_private_json
from .agent_channels import credential_present, resolve, validate_binding
from .cli_agents import AGENTS as CLI_AGENTS
from .core import PolicyError, now

ROLES = ("taizi", "zhongshu", "menxia", "shangshu", "gongbu")

# 太子/尚书是高频低成本位；中书/工部要 schema 稳定。
# 门下必须与中书不同厂商，否则封驳成为同源审查。
ROLE_LABELS = {"taizi": "太子", "zhongshu": "中书", "menxia": "门下",
               "shangshu": "尚书", "gongbu": "工部"}
RECOMMENDATION_REASONS = {
    "taizi": "高频分拣与规范化，优先低延迟、低成本；Hermes 的 profile/工具生态适合反复判定。",
    "zhongshu": "规划要产出稳定 schema 与可验收子任务；Codex 的结构化输出最稳，适合做主计划。",
    "menxia": "封驳必须独立于中书；Claude 与 Codex 不同厂商，长上下文审查更强，优先保证独立性。",
    "shangshu": "只做依赖排序与派发，不需要昂贵模型；Pi 快且便宜，失败可重试。",
    "gongbu": "真实写文件、按 schema 交付；Codex 的代码/结构化能力最适合工部产出。",
}
RECOMMENDATION_ALTERNATIVES = {
    "taizi": [{"agent": "pi", "model": "deepseek-flash", "why": "更便宜，但工具/记忆生态弱于 Hermes"}],
    "zhongshu": [{"agent": "claude", "model": "claude-sonnet-5-5", "why": "长上下文更强；注意与门下同源"}],
    "menxia": [{"agent": "hermes", "model": "gpt-6.1-terra", "why": "备选审查；与中书同源时独立性下降"}],
    "shangshu": [{"agent": "hermes", "model": "gpt-6.1-terra", "why": "更稳但更贵，派发通常不需要"}],
    "gongbu": [{"agent": "claude", "model": "claude-sonnet-5-5", "why": "长文/说明类产出更好；代码任务仍优先 Codex"}],
}
DEFAULT_PRESET = {
    "taizi": {"agent": "hermes", "model": "gpt-6.1-terra"},
    "zhongshu": {"agent": "codex", "model": "gpt-6.1-sol"},
    "menxia": {"agent": "claude", "model": "claude-sonnet-5-5"},
    "shangshu": {"agent": "pi", "model": "deepseek-flash"},
    "gongbu": {"agent": "codex", "model": "gpt-6.1-sol"},
}


def recommendations():
    """Role-level defaults with explicit trade-offs; not a capability claim."""
    return [{"role": role, "label": ROLE_LABELS[role], **DEFAULT_PRESET[role],
             "reason": RECOMMENDATION_REASONS[role],
             "alternatives": list(RECOMMENDATION_ALTERNATIVES[role])}
            for role in ROLES]


def preset_status(probe_result):
    return [{"role": role, **entry,
             "found": bool(probe_result.get(entry["agent"], {}).get("found")),
             "path": probe_result.get(entry["agent"], {}).get("path")}
            for role, entry in DEFAULT_PRESET.items()]


def apply_preset(probe_result, instance=None):
    """Build an explicit partial preset; optionally atomically persist it.

    Missing roles have no bindings (including stale bindings from older files).
    This preference does not grant provider or execution authority.
    """
    states = preset_status(probe_result)
    value = {"mode": "multi", "bindings": {
        row["role"]: {"agent": row["agent"], "model": row["model"]}
        for row in states if row["found"]}, "updated_at": now()}
    if instance is not None:
        for entry in value["bindings"].values():
            validate_binding(entry["agent"], entry["model"], instance=instance)
        write_private_json(Path(instance) / "roles.json", value)
        value["warnings"] = warnings(value, instance=instance)
    return {**value, "missing": [row["role"] for row in states if not row["found"]]}


def default():
    return {"mode": "single", "bindings": {role: {"agent": "codex", "model": MODEL}
                                           for role in ROLES}, "updated_at": None}


def check_independence(binding):
    """Reject a multi-role plan/review pair from the same agent+model.

    Single-agent demo mode is explicit and remains allowed (with warnings);
    a multi-role binding must keep 中书 and 门下 independent.
    """
    if not isinstance(binding, dict) or binding.get("mode") != "multi":
        return
    entries = binding.get("bindings")
    if not isinstance(entries, dict):
        return
    plan, review = entries.get("zhongshu"), entries.get("menxia")
    if not isinstance(plan, dict) or not isinstance(review, dict):
        return
    if (plan.get("agent"), plan.get("model")) == (review.get("agent"), review.get("model")):
        raise PolicyError("门下必须与中书不同源：multi 模式下 agent+model 不得相同")


def check_gongbu_backend(binding):
    """Refuse a tool-owning CLI backend for the file-writing 工部 role.

    CLI agents bring their own tools and host environment; the runtime boundary
    is not a sandbox. API channels remain the supported 工部 path. This is a
    hard policy, not a warning, because 工部 writes artifacts.
    """
    if not isinstance(binding, dict):
        return
    entries = binding.get("bindings")
    if not isinstance(entries, dict):
        return
    gongbu = entries.get("gongbu")
    if isinstance(gongbu, dict) and gongbu.get("agent") in CLI_AGENTS:
        raise PolicyError("工部不得绑定带工具的 CLI agent；请使用 API 通道（codex/hermes/pi/claude）")


def load(instance):
    try:
        value = json.loads((Path(instance) / "roles.json").read_text())
    except FileNotFoundError:
        return default()
    check_independence(value)
    check_gongbu_backend(value)
    return value


def validate(binding, agents=None, *, instance=None):
    """Raise ValueError for malformed/incomplete/undetected bindings."""
    agents = probe() if agents is None else agents
    if not isinstance(binding, dict) or binding.get("mode") not in ("single", "multi"):
        raise ValueError("mode must be single or multi")
    entries = binding.get("bindings")
    if not isinstance(entries, dict) or set(entries) != set(ROLES):
        raise ValueError("角色必须齐全且无多余角色: " + ", ".join(ROLES))
    for role, entry in entries.items():
        if not isinstance(entry, dict) or set(entry) != {"agent", "model"}:
            raise ValueError(f"{role}: binding requires agent and model")
        if role == "gongbu" and entry["agent"] in CLI_AGENTS:
            raise ValueError("工部不得绑定带工具的 CLI agent；请使用 API 通道")
        agent = entry["agent"]
        validate_binding(agent, entry["model"], instance=instance)
        if not isinstance(agent, str) or not agents.get(agent, {}).get("found"):
            raise ValueError(f"{role}: 未检测到的 agent")
        if not isinstance(entry["model"], str) or not entry["model"].strip():
            raise ValueError(f"{role}: model must be nonempty")
    if binding["mode"] == "single" and len({(e["agent"], e["model"]) for e in entries.values()}) != 1:
        raise ValueError("single 模式必须全部角色使用同一 agent+model")
    return binding


def warnings(binding, agents=None, *, instance=None):
    agents = probe() if agents is None else agents
    entries = binding.get("bindings", {}) if isinstance(binding, dict) else {}
    entries = entries if isinstance(entries, dict) else {}
    result = []
    plan, review = entries.get("zhongshu"), entries.get("menxia")
    if isinstance(plan, dict) and isinstance(review, dict) and plan == review:
        result.append("审查独立性弱：门下省与中书省使用同一 agent+model")
    # Missing credentials are a warning, not a bind-time error: setup must not
    # be blocked on keys, but the panel has to say the channel cannot run yet.
    try:
        for role, entry in entries.items():
            if not isinstance(entry, dict) or not isinstance(entry.get("agent"), str):
                continue
            try:
                _, _, key_env, _ = resolve(entry["agent"], entry.get("model"), instance=instance)
            except Exception:
                continue
            if key_env and not credential_present(key_env):
                result.append("%s 绑定的 %s 缺少凭据环境变量 %s：可保存，但运行会失败"
                              % (role, entry["agent"], key_env))
    except Exception:
        pass
    valid = [entry for entry in entries.values() if isinstance(entry, dict)]
    names = [entry.get("agent") for entry in valid if isinstance(entry.get("agent"), str)]
    if len(names) == len(ROLES) and len(set(names)) == 1:
        result.append("单 agent 承担全部角色")
    for agent in sorted(set(names)):
        if not agents.get(agent, {}).get("found"):
            result.append("绑定了未检测到的 agent: " + agent)
    return result


def save(instance, binding, agents=None):
    agents = probe(instance) if agents is None else agents
    check_independence(binding)
    check_gongbu_backend(binding)
    validate(binding, agents, instance=instance)
    value = deepcopy(binding)
    value["updated_at"] = now()
    write_private_json(Path(instance) / "roles.json", value)
    # Callers surface these to the operator; a saved binding with a missing
    # credential is valid config that cannot run yet.
    return {**value, "warnings": warnings(value, agents, instance=instance)}
