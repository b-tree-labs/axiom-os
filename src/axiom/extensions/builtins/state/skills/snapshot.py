# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``state.snapshot`` -- take a snapshot of the harness state now."""

from __future__ import annotations

from typing import Any

from axiom.infra import state_snapshots as ss
from axiom.infra.skills import SkillContext, SkillResult

from ._common import cli, coverage_lines, resolve


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    state_dir, root, policy, _store = resolve(params, ctx)
    try:
        r = ss.create_snapshot(
            state_dir, root,
            reason=params.get("reason") or "on-demand",
            trigger=params.get("trigger") or "manual",
            policy=policy,
            force=bool(params.get("force")),
            with_db=bool(params.get("with_db")),
            db_url=params.get("db_url"),
            yes_this_database=bool(params.get("yes_this_database")),
            prune=False if params.get("no_prune") else None,
            _db_dry_run=bool(params.get("_db_dry_run")),
        )
    except ss.DiskPressureError as exc:
        return SkillResult(ok=False, errors=[str(exc)], actions_taken=[], value={
            "next_steps": [f"{cli()} state ls", f"{cli()} state prune --apply",
                           f"{cli()} state snapshot --to /path/on/another/disk"]})
    except ss.SnapshotError as exc:
        return SkillResult(ok=False, errors=[str(exc)])
    c = cli()
    next_steps = [f"{c} state show {r.snapshot_id}", f"{c} state verify {r.snapshot_id}",
                  f"{c} state restore {r.snapshot_id}   (dry run; add --apply to restore)"]
    if r.skipped:
        next_steps = [f"{c} state ls", f"{c} state snapshot --force   (take one anyway)"]
    return SkillResult(
        ok=True,
        value={**ss.snapshot_dict(r), "coverage": coverage_lines(root, state_dir), "next_steps": next_steps},
        actions_taken=[r.message, *[f"pruned {p}" for p in r.pruned]],
    )
