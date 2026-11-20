# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``state.reconcile`` -- bring a retired state folder into the one store. Dry run unless ``apply``."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

from .. import reconcile as rc
from ._common import cli


def pairs(ctx: SkillContext) -> list[tuple[str, Path, Path]]:
    """``(scope, old folder, current folder)`` for each retired folder present."""
    from axiom.infra.brand_migration import retired_state_dirs
    from axiom.infra.paths import get_project_root

    return [
        (d.scope, d.legacy, d.current)
        for d in retired_state_dirs(home=Path.home(), state_dir=Path(ctx.state_dir),
                                    project_root=get_project_root())
    ]


def _journal(ctx: SkillContext, params: dict[str, Any], res: SkillResult) -> SkillResult:
    """Journal an apply. The skill is registered unaudited so a dry run, which
    writes nothing, adds no row; the apply changes the settings store and does."""
    from axiom.infra import audit_trail

    audit_trail.emit(ctx, skill="state.reconcile", params=params,
                     outcome="ok" if res.ok else "error", errors_count=len(res.errors))
    return res


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    c = cli()
    prefer = params.get("prefer") or "newest"
    if prefer not in rc.PREFER:
        return SkillResult(ok=False, errors=[f"prefer must be one of {', '.join(rc.PREFER)}, not {prefer!r}"])
    apply = bool(params.get("apply"))
    plans = rc.plan(pairs(ctx), prefer)
    value: dict[str, Any] = {
        "applied": False,
        "prefer": prefer,
        "folders": [fp.as_dict() for fp in plans],
        "conflicts": sum(len(a.conflicts) for fp in plans for a in fp.actions),
        "archive": None,
        "markers": [],
        "notice": "",
    }
    pending = [a for fp in plans for a in fp.actions
               if a.action in ("copy", "keep-both") or (a.action == "merge" and a.writes)]
    if not plans:
        value["next_steps"] = []
        return SkillResult(ok=True, value=value, actions_taken=["no old state folder found; nothing to reconcile"])
    if not apply:
        value["next_steps"] = (
            [f"{c} state reconcile --apply", f"{c} state reconcile --apply --prefer current|legacy"]
            if pending else []
        )
        return SkillResult(ok=True, value=value, actions_taken=[
            f"dry run: {len(pending)} file(s) to bring across, {value['conflicts']} conflict(s); nothing written"])
    try:
        done = rc.apply(plans, Path(ctx.state_dir) / "archive")
    except OSError as exc:
        return _journal(ctx, params, SkillResult(ok=False, value=value, errors=[
            f"reconcile stopped part way: {exc}. The old folder is unchanged; "
            "anything already written was archived first."]))
    value.update(applied=True, archive=done["archive"], markers=done["markers"], notice=rc.notice(plans, c))
    value["next_steps"] = [f"{c} state reconcile   (should list nothing left to bring across)"]
    return _journal(ctx, params, SkillResult(ok=True, value=value, actions_taken=[
        f"archived to {done['archive']}; brought {len(pending)} file(s) across, "
        f"{value['conflicts']} conflict(s) resolved by {prefer}"]))
