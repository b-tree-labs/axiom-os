# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``state.ls`` -- list snapshots (no hashing; fast)."""

from __future__ import annotations

from typing import Any

from axiom.infra import state_snapshots as ss
from axiom.infra.skills import SkillContext, SkillResult

from ._common import cli, coverage_lines, resolve


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    state_dir, root, policy, store = resolve(params, ctx)
    rows = ss.list_snapshots(store)
    c = cli()
    return SkillResult(ok=True, value={
        "root": str(root), "snapshots": rows,
        "interval_hours": policy.interval_hours, "enabled": policy.enabled,
        "coverage": coverage_lines(root, state_dir),
        "next_steps": ([f"{c} state show {rows[0]['id']}", f"{c} state verify", f"{c} state prune"]
                       if rows else [f"{c} state snapshot"]),
    })
