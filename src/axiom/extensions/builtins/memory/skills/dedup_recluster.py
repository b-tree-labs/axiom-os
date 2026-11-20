# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""``memory.dedup_recluster`` skill — the corpus-health re-cluster pass
(ADR-087 D3, clock 2).

Invocable-only in P2: **no scheduler wiring** — cadence policy is a
deliberate knob deferral (wiring it into a scheduler is a one-liner
once decided). Runs blocking → matching → clustering →
canonicalization over one principal's live memory: exact/near-dup
clusters fold reversibly into their earliest fragment, ambiguous pairs
queue as kept-both conflicts, open conflicts and vault are never
touched. Idempotent — re-running over a resolved corpus does nothing.

Matching defaults to the offline lexical engine; inject
``dedup_engine`` for an embedder-backed pass.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from axiom.infra.skills import SkillContext, SkillResult


def dedup_recluster(
    params: dict[str, Any], ctx: SkillContext | None
) -> SkillResult:
    """Run one entity-resolution pass over a principal's memory."""
    composition = params.get("composition")
    if composition is None:
        return SkillResult(ok=False, errors=["no composition service provided"])
    principal = params.get("principal")
    if not principal:
        return SkillResult(
            ok=False,
            errors=["--principal is required: recluster is per-principal"],
        )

    from axiom.memory.dedup import DedupEngine, recluster

    engine = params.get("dedup_engine")
    if engine is None:
        engine = DedupEngine(embedder=None)

    report = recluster(
        composition,
        principal=principal,
        engine=engine,
        dry_run=bool(params.get("dry_run", False)),
    )
    value = asdict(report)
    actions = []
    if not report.dry_run and (report.merged or report.conflicts_queued):
        actions.append(
            f"reclustered {principal}: merged {report.merged}, "
            f"queued {report.conflicts_queued} conflict(s)"
        )
    return SkillResult(ok=True, value=value, actions_taken=actions)
