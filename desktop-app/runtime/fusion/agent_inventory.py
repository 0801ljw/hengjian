"""Read-only inventory for the Agent Registry.

This is the machine-readable view used by the DSH plugin and the panel. It
includes local discovery, readiness, supported roles and current bindings, but
never API keys, environment values or install audit data.
"""
from __future__ import annotations

from .agent_catalog import catalog
from .agent_channels import CHANNELS
from .agent_health import health
from .agent_workspace import inventory_fields, runs
from .cli_agents import AGENTS as CLI_AGENTS
from .role_binding import ROLE_LABELS, load


def inventory_payload(instance) -> dict:
    from .agent_install import installability
    from .agents_probe import probe

    facts = probe(instance)
    try:
        binding = load(instance)
    except Exception:
        binding = {"mode": "single", "bindings": {}}
    bound = {}
    for role, entry in (binding.get("bindings") or {}).items():
        if isinstance(entry, dict) and isinstance(entry.get("agent"), str):
            bound.setdefault(entry["agent"], []).append(role)

    rows = []
    for row in catalog(facts, installability(), instance):
        agent_id = row.get("id")
        if not isinstance(agent_id, str):
            continue
        state = health(agent_id, facts, instance)
        roles = sorted(bound.get(agent_id, []))
        backend = "cli" if agent_id in CLI_AGENTS else "http" if agent_id in CHANNELS else "custom"
        workspace = last_issue = None
        if row.get("found") or roles:
            try:
                fields = inventory_fields(instance, agent_id, roles[0] if roles else agent_id)
                workspace = fields["workspace"]
                last_issue = fields["last_issue"]
            except Exception:
                pass
        rows.append({
            "id": agent_id,
            "title": row.get("title") or agent_id,
            "vendor": row.get("vendor") or "",
            "description": row.get("description") or "",
            "backend": backend,
            "integrated": bool(row.get("integrated")),
            "found": bool(row.get("found")),
            "version": row.get("version"),
            "path": row.get("path"),
            "supported_roles": list(row.get("roles") or ()),
            "bound_roles": roles,
            "bound_labels": [ROLE_LABELS.get(role, role) for role in roles],
            "health": state,
            "ready": bool(state.get("ready")),
            "installable": bool(row.get("installable")),
            "docs": row.get("docs"),
            "reason": row.get("reason"),
            "models": row.get("models") if isinstance(row.get("models"), list) else [],
            "workspace": workspace,
            "last_issue": last_issue,
            "last_run": (runs(instance, agent_id, 1) or [None])[0] if row.get("found") else None,
        })

    return {
        "agents": rows,
        "binding": binding,
        "discovered": sum(1 for row in rows if row["found"]),
        "ready": sum(1 for row in rows if row["ready"]),
        "bound": sum(1 for row in rows if row["bound_roles"]),
        "mode": binding.get("mode"),
    }
