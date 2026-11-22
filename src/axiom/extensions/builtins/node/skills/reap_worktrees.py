# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``node.reap_worktrees``: finished git worktrees, planned by default, removed on ``apply``."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

from .. import reap
from ._handoff import next_step
from .inventory import _state_dir


def run(params: dict[str, Any], ctx: SkillContext | None = None) -> SkillResult:
    state = _state_dir(ctx)
    cfg = reap.load_config(
        state,
        min_age_hours=params.get("min_age_hours"),
        batch=params.get("batch"),
        retention_days=params.get("retention_days"),
    )
    roots = [params["root"]] if params.get("root") else (cfg.roots or [])
    if not roots:
        if params.get("heartbeat"):
            return SkillResult(
                ok=True,
                value={"mode": "skipped", "note": "no workspace root configured"},
                actions_taken=["no workspace root configured; nothing reaped"],
            )
        roots = [str(Path.cwd())]
    apply = bool(params.get("apply"))
    value: dict[str, Any] = {"roots": roots, "removed": [], "would_remove": [], "skipped": [],
                             "failed": [], "expired_archives": []}
    for r in roots:
        part = reap.reap(Path(r), state, cfg, apply=apply)
        value["mode"] = part["mode"]
        for k in ("removed", "would_remove", "skipped", "failed", "expired_archives"):
            value[k] += part[k]
    n = len(value["removed"]) if apply else len(value["would_remove"])
    return SkillResult(
        ok=True,
        value=value,
        actions_taken=[
            f"removed {n} finished worktree(s); branches kept"
            if apply
            else f"planned {n} removal(s); nothing changed"
        ],
        next=[next_step("node.vitals", "confirm the headroom after the worktrees are gone")]
        if apply and n
        else [],
    )


__all__ = ["run"]
