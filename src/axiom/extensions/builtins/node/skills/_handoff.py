# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""How KEEL hands work to another crew member, and what it says when it cannot.

A hand-off is a registry invocation of another agent's skill, carrying the
baton (:mod:`axiom.infra.skill_chain`): the chain id, the hop count and the
skills visited so far, so a sequence that circles back is bounded and named
rather than endless. KEEL never messages anyone itself; HERALD does. KEEL
never touches a credential; KEEP does. This module is the one place those
calls are made, so the hand-off table in the README is also the code.
"""

from __future__ import annotations

import logging
from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

log = logging.getLogger(__name__)

#: Adjacent skills KEEL hands to, by role. The registry decides whether each is present.
HERALD_ALERT = "notifications.alert"
KEEP_EXPOSED = "vault.exposed"
KEEP_ROTATE = "vault.rotate"
UPDATE_CHECK = "update.check"
PLINTH_STORAGE = "data.storage"
FLEET_REPORT = "fleet.report"
WHOAMI = "whoami.report"


def reachable(ctx: SkillContext | None, name: str) -> bool:
    """Is the other crew member on this surface's registry? (The CLI binds the whole
    platform; a narrow test registry may hold only KEEL, and that is a fact, not an error.)"""
    reg = getattr(ctx, "registry", None)
    return bool(reg is not None and reg.has(name))


def handoff(ctx: SkillContext | None, name: str, params: dict[str, Any]) -> dict[str, Any]:
    """Invoke ``name`` with the baton; return a small record of what happened.

    Never raises: an unreachable or failing hand-off is reported in the record so
    the calling skill can say "HERALD was not reachable from here" instead of
    pretending the alert went out.
    """
    if not reachable(ctx, name):
        return {
            "target": name,
            "status": "unreachable",
            "detail": f"{name} is not registered on this surface",
        }
    try:
        from axiom.infra import skill_chain
        from axiom.infra.skill_dispatch import invoke_capability

        baton = skill_chain.baton_for(ctx)
        spec = ctx.registry.spec(name)  # type: ignore[union-attr]
        why = baton.refusal(
            ctx, name, params, writes=bool(spec is None or spec.effective_risk != "read")
        )
        if why:
            return {"target": name, "status": "refused", "detail": why}
        surface = getattr(ctx, "surface", None) or "cli"
        result: SkillResult = invoke_capability(
            ctx.registry, name, dict(params), ctx, surface=surface, chain=baton.passing_to(name)
        )  # type: ignore[union-attr]
    except Exception as exc:  # noqa: BLE001 - a hand-off that fails is a finding, never a crash
        log.warning("hand-off to %s failed: %s", name, exc)
        return {"target": name, "status": "failed", "detail": f"{type(exc).__name__}: {exc}"}
    return {
        "target": name,
        "status": "ok" if result.ok else "failed",
        "detail": "; ".join(result.errors) if result.errors else "",
        "value": result.value,
    }


def next_step(target: str, why: str, when: str = "ok") -> dict[str, str]:
    """A follow-up the caller may take (ADR-196): the skill to call and why, in one line.

    ``when`` is the platform's fixed vocabulary (``ok``, ``error``, ``always``,
    ``produced:<kind>``); the circumstance belongs in ``why``.
    """
    return {"target": target, "when": when, "why": why}


__all__ = [
    "FLEET_REPORT",
    "HERALD_ALERT",
    "KEEP_EXPOSED",
    "KEEP_ROTATE",
    "PLINTH_STORAGE",
    "UPDATE_CHECK",
    "WHOAMI",
    "handoff",
    "next_step",
    "reachable",
]
