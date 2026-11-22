"""Narrow, documented permission grants layered on top of the frozen upstream matrix.

`scripts/kanban_update.py` (imported as `kanban_update`) is a frozen asset: its
AGENT_POLICY and transition table are asserted by regression tests. Rather than
editing that file, fusion declares the two grants it needs to make cancellation
work end to end, and enforces their limits here:

  gongbu  : "cancel"  -> may only request Doing -> Cancelled (still high-risk,
                         still needs shangshu confirmation). It does NOT gain a
                         general "state" command, so it cannot route its own work
                         to Done.
  zhongshu: "confirm" -> it is already CONFIRM_AUTHORITY for Menxia->Cancelled;
                         this lets it actually answer that prompt. Other
                         confirmations belong to menxia/shangshu and stay theirs.

Every grant is validated at import time so a typo cannot widen it.
"""
from __future__ import annotations

import kanban_update as kb

from .core import PolicyError

GRANTS: dict[str, set[str]] = {
    "gongbu": {"cancel"},
    "zhongshu": {"confirm"},
}

_capabilities = {"cancel": "cancellation", "confirm": "confirmation"}


def _validate() -> None:
    for actor, commands in GRANTS.items():
        base = kb.AGENT_POLICY.get(actor)
        if base is None:
            raise RuntimeError(f"grant for unknown actor {actor}")
        for command in commands:
            # A grant may only add commands the actor does not already have, and
            # only commands this module knows how to constrain.
            if command in base["commands"]:
                raise RuntimeError(f"{actor} already has {command}; drop the grant")
            if command not in _capabilities:
                raise RuntimeError(f"ungrantable command {command}")


_validate()


def granted(actor: str, command: str) -> bool:
    return command in GRANTS.get(actor, set())


def check_cancel(actor: str, old: str, target: str) -> None:
    """`cancel` is only ever a request to move *own* task to Cancelled."""
    if target != "Cancelled":
        raise PolicyError("cancel command may only target Cancelled")
    if old not in kb._VALID_TRANSITIONS or "Cancelled" not in kb._VALID_TRANSITIONS[old]:
        raise PolicyError(f"illegal cancellation from {old}")
    if kb._STATE_AGENT_MAP.get(old) != actor and old not in ("Doing", "Next"):
        raise PolicyError(f"{actor} does not own state {old}")
    if old in ("Doing", "Next") and actor != "gongbu":
        raise PolicyError("only the executing agent may cancel running work")


def check_block(actor: str, old: str, target: str) -> None:
    """`block` may only move the actor's own task to Blocked.

    The frozen matrix already grants execution roles the `block` command and
    already contains Doing/Assigned/Next -> Blocked. Ownership is enforced by
    the mutation service (`_owner`), so this guard only constrains the target
    and the transition itself.
    """
    if target != "Blocked":
        raise PolicyError("block command may only target Blocked")
    if old not in kb._VALID_TRANSITIONS or "Blocked" not in kb._VALID_TRANSITIONS[old]:
        raise PolicyError(f"illegal block from {old}")


__all__ = ["GRANTS", "granted", "check_cancel", "check_block"]
