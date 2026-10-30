# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``state.prune`` -- apply retention.  Dry run unless ``apply``."""

from __future__ import annotations

from typing import Any

from axiom.infra import state_snapshots as ss
from axiom.infra.skills import SkillContext, SkillResult

from ._common import cli, resolve


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    _sd, _root, policy, store = resolve(params, ctx)
    apply = bool(params.get("apply"))
    try:
        out = ss.prune_store(store, policy, apply=apply, pressure=bool(params.get("harder")))
    except ss.SnapshotError as exc:
        return SkillResult(ok=False, errors=[str(exc)])
    c = cli()
    p = out.plan
    return SkillResult(ok=True, value={
        "applied": out.applied,
        "keep": [{"id": d.id, "reasons": list(d.reasons)} for d in p.keep],
        "delete": [{"id": d.id, "reasons": list(d.reasons)} for d in p.delete],
        "warnings": list(p.warnings), "kept_bytes": p.kept_bytes, "over_budget": p.over_budget,
        "bytes_freed": out.bytes_freed,
        "next_steps": ([f"{c} state prune --apply"] if p.delete and not apply else [f"{c} state ls"]),
    }, actions_taken=[f"{'pruned' if apply else 'would prune'} {len(p.delete)} snapshot(s); keeping {len(p.keep)}"])
