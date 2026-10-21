# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``state.diff`` -- what restoring a snapshot would change right now."""

from __future__ import annotations

from typing import Any

from axiom.infra import state_snapshots as ss
from axiom.infra.skills import SkillContext, SkillResult

from ._common import cli, resolve


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    state_dir, _root, _policy, store = resolve(params, ctx)
    try:
        sid = ss.resolve_id(store, params.get("id") or "latest")
        diff = ss.diff_snapshot(store, sid, state_dir, only=params.get("only") or ())
    except ss.SnapshotError as exc:
        return SkillResult(ok=False, errors=[str(exc)])
    changes = [d for d in diff if d.action not in ("unchanged", "kept")]
    kept = [d.path for d in diff if d.action == "kept"]
    c = cli()
    return SkillResult(ok=True, value={
        "id": sid, "changes": [ss.snapshot_dict(d) for d in changes], "unchanged": len(diff) - len(changes) - len(kept),
        "kept": kept,
        "next_steps": [f"{c} state restore {sid} --apply" if changes else f"{c} state ls"],
    })
