# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``program.followup_fire`` and ``program.followup_sweep`` (ADR-196).

The two actions the scheduler runs. Both are CLI-only: the orchestrator reaches
them through its skill registry (an action string is a qualified skill name),
and no harness has a reason to fire one by hand. ``followup_fire`` is
idempotent, because PULSE re-runs a fire whose process was killed.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

from .. import followup_engine as engine

#: Test seam, as in followups.py.
_now: Callable[[], datetime] = lambda: datetime.now(UTC)  # noqa: E731


def fire(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """Run one follow-up's next rung: check, compose, deliver, escalate on silence."""
    fid = str(params.get("followup_id") or "").strip()
    if not fid:
        return SkillResult(ok=False, errors=["followup_id is required"])
    out = engine.fire(fid, registry=ctx.registry, state_dir=ctx.state_dir, now=_now())
    return SkillResult(ok=True, value=out, actions_taken=[f"{fid}: {out['outcome']}"])


def sweep(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """Re-arm lost clocks, expire stale follow-ups, mark never-delivered ones missed. Quiet when idle."""
    out = engine.sweep(now=_now())
    actions = [] if out["quiet"] else [f"{k}: {', '.join(v)}" for k, v in out.items() if k != "quiet" and v]
    return SkillResult(ok=True, value=out, actions_taken=actions)


__all__ = ["fire", "sweep"]
