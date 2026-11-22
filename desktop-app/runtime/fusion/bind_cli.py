"""Local discovery and opt-in, editable heuristic role suggestions."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .agents_probe import probe, redact
from .ai import MODEL
from .agent_channels import CHANNELS
from . import role_binding

# 启发式，可用 --set 覆盖。These are policy preferences, NOT detected capabilities.
SUGGESTIONS = {
    "taizi": ("classify", "判定任务还是闲聊，短输出"),
    "zhongshu": ("plan", "规划质量决定后面全链条"),
    "menxia": ("review", "独立审查，最好换不同来源"),
    "shangshu": ("plan", "只是排序，低成本即可"),
    "gongbu": ("code", "要产出真实文件内容"),
}
PREFERENCES = {"classify": ("pi",), "plan": ("codex", "hermes"),
               "review": ("claude", "codex"), "write": ("claude",), "code": ("codex",)}


def suggest(agents):
    available = [name for name, facts in agents.items() if facts.get("found") and name != "api"]
    if not available and agents.get("api", {}).get("found"):
        available = ["api"]
    entries, reasons = {}, {}
    for role, (tag, reason) in SUGGESTIONS.items():
        choices = [name for name in PREFERENCES[tag] if name in available]
        choices += [name for name in available if name not in choices]
        if role == "menxia" and "zhongshu" in entries:
            # Independence is an explicit preference, never an ability claim.
            source = entries["zhongshu"]["agent"]
            choices.sort(key=lambda name: name == source)
        if not choices:
            reasons[role] = reason + "；无已检测到的 agent，未生成绑定"
            continue
        agent = choices[0]
        observed = agents[agent].get("model")
        model = observed or CHANNELS.get(agent, {}).get("default_model", MODEL)
        entries[role] = {"agent": agent, "model": model}
        source = "配置中读取的 model" if observed else "建议 model，非探测事实，请自行确认可用性"
        reasons[role] = reason + f"；按启发式选择 {agent}；{source}"
    mode = "single" if len({(e["agent"], e["model"]) for e in entries.values()}) <= 1 else "multi"
    return {"mode": mode, "bindings": entries, "updated_at": None}, reasons


def _entry(value):
    agent, separator, model = value.partition(":")
    if not separator or not agent.strip() or not model.strip():
        raise ValueError("绑定格式必须为 agent:model")
    return {"agent": agent.strip(), "model": model.strip()}


def _print_warnings(binding, agents):
    for message in role_binding.warnings(binding, agents):
        print("警告：" + redact(message))


def main(argv=None):
    parser = argparse.ArgumentParser(prog="fusion", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("agents", "bind"):
        command = sub.add_parser(name)
        command.add_argument("--instance", "--home", type=Path, default=Path("instance"),
                             help="instance directory (same as fusion run --home)")
        if name == "agents":
            command.add_argument("--refresh", action="store_true")
        else:
            command.add_argument("--show", action="store_true")
            command.add_argument("--mode", choices=("single", "multi"))
            command.add_argument("--agent")
            command.add_argument("--set", action="append", default=[], metavar="ROLE=AGENT:MODEL")
            command.add_argument("--auto", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "bind" and args.show:
            if args.mode or args.agent or args.set or args.auto:
                raise ValueError("--show 不能与写入参数组合")
            binding = role_binding.load(args.instance)
            print(json.dumps(redact(binding), ensure_ascii=False, indent=2))
            _print_warnings(binding, probe(args.instance))
            return 0
        agents = probe(args.instance, refresh=getattr(args, "refresh", False))
        proposed, reasons = suggest(agents)
        if args.command == "agents":
            print("探测事实（不推断能力；env_key 仅表示环境变量非空；缓存有效期 24h）")
            print("agent\tfound\tpath\tversion\tconfig_exists\tenv_key")
            for name, facts in agents.items():
                print("\t".join(str(item) for item in (name, facts["found"], facts["path"],
                      facts["version"], facts["config_exists"], facts["env_key"])))
            print(json.dumps(agents, ensure_ascii=False, indent=2))
        elif args.auto:
            if args.agent:
                raise ValueError("--auto 不支持 --agent；请用 --set 覆盖")
        elif args.mode == "single":
            if not args.agent or args.set:
                raise ValueError("single 需要 --agent，且不支持 --set；逐角色设置请用 multi")
            proposed = {"mode": "single", "bindings": {role: _entry(args.agent)
                        for role in role_binding.ROLES}, "updated_at": None}
        elif args.mode == "multi":
            if args.agent:
                raise ValueError("multi 请用 --set，不支持 --agent")
            # New multi configurations must explicitly cover all roles. On edits,
            # retain only the user's existing saved choices (not default guesses).
            proposed = (role_binding.load(args.instance) if (args.instance / "roles.json").exists()
                        else {"bindings": {}})
            proposed["mode"] = "multi"
        else:
            raise ValueError("请指定 --show、--auto 或 --mode")
        if args.command == "bind":
            for setting in args.set:
                role, separator, value = setting.partition("=")
                if not separator or role not in role_binding.ROLES:
                    raise ValueError("--set 需要合法 ROLE=AGENT:MODEL")
                proposed["bindings"][role] = _entry(value)
            if args.auto:
                if args.mode:
                    proposed["mode"] = args.mode
                for setting in args.set:
                    role = setting.partition("=")[0]
                    reasons[role] = "用户 --set 覆盖启发式；" + SUGGESTIONS[role][1]
        if args.command == "agents" or args.auto:
            print("建议绑定：启发式，可用 --set 覆盖（不是能力或价格探测结论）")
            for role, reason in reasons.items():
                print(redact(f"{role}: {proposed['bindings'].get(role)} — {reason}"))
        _print_warnings(proposed, agents)
        if args.command == "bind":
            saved = role_binding.save(args.instance, proposed, agents)
            print(json.dumps(redact(saved), ensure_ascii=False, indent=2))
            print("已写入 roles.json（0600）；按 agent 路由协议和通道，不启用 CLI 执行。")
        return 0
    except (ValueError, TypeError, OSError, KeyError) as exc:
        # Do not echo exception strings from raw JSON/config inputs.
        print("错误：" + (redact(str(exc)) if isinstance(exc, ValueError) and not isinstance(exc, json.JSONDecodeError)
                         else type(exc).__name__))
        return 2
