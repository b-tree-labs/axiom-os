# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``hygiene reclaim`` — surface the lanes TIDY could reclaim. Propose-only.

This is the agent-reachable face of ``hygiene.lane_reclaim.assess`` (ADR-046:
TIDY removes the brown). It is deliberately read-only on every surface — CLI,
MCP, agent-tool — because the asymmetry the reclaim module was built around
does not change just because an agent asked: a lane released by mistake is
re-claimed in a second, a database dropped by mistake is somebody's afternoon.
So this returns the proposal and never the act (ADR-141 — the agent halves of
``lane`` propose; the deterministic ``axi lane`` is where a human runs the
drop).

Two extensions meet here and neither is required. ``lane`` supplies the
registry of what exists; ``release`` (RIVET) supplies the one judgement TIDY
is forbidden to make for itself — whether a branch has landed. If either is
absent the skill degrades rather than failing: no ``lane`` means nothing to
assess, no ``release`` means every branch reads as *cannot tell* and blocks.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

from .. import lane_reclaim


def _workspace() -> Path:
    """Where sibling checkouts and their venvs live, for the F2 bound-floor."""
    root = os.getenv("AXI_WORKSPACE_ROOT")
    return Path(root).expanduser() if root else Path.cwd()


def _load_lanes() -> dict:
    """The lane registry, or an empty map when the lane extension is absent."""
    from axiom.extensions.builtins.lane.registry import Registry
    from axiom.extensions.builtins.lane.skills import lanes_path

    return Registry(lanes_path()).all()


def _landed_probe(lanes: dict):
    """RIVET's branch-landed judgement, injected. ``None`` when RIVET is absent.

    Passing ``None`` to ``assess`` is not a degraded guess — it is the honest
    one. ``assess`` reads a missing probe as *cannot tell* for every branch,
    which blocks, which is the safe direction. TIDY never substitutes its own
    merge check here; that authority is RIVET's (ADR-046).
    """
    try:
        from axiom.extensions.builtins.release import lane_signal
    except Exception:
        return None
    return lane_signal.landed_probe(lanes)


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """``axi hygiene reclaim`` — which lanes look reclaimable, and which must not be touched."""
    try:
        lanes = _load_lanes()
    except Exception as exc:
        return SkillResult(
            ok=True,
            value={"reclaimable": [], "blocked": [], "text": "no lane registry — nothing to reclaim"},
            actions_taken=[f"the lane extension is not available ({type(exc).__name__}); nothing to reclaim"],
        )

    items = lane_reclaim.assess(
        lanes, workspace=_workspace(), branch_landed=_landed_probe(lanes)
    )
    text = lane_reclaim.render(items)

    def _row(r: lane_reclaim.Reclaim) -> dict[str, Any]:
        return {
            "lane": r.lane,
            "why": list(r.why),
            "proposed": list(r.proposed),
            "blocked_by": list(r.blocked_by),
            "actionable": r.actionable,
        }

    value = {
        "reclaimable": [_row(r) for r in items if r.actionable],
        "blocked": [_row(r) for r in items if not r.actionable],
        "text": text,
    }
    # text in actions_taken so the thin CLI renders it; the structured value is
    # the MCP/agent payload. Nothing here acts, so actions_taken is the report,
    # not a record of changes — reclaim proposes only.
    return SkillResult(ok=True, value=value, actions_taken=[text])


__all__ = ["run"]
