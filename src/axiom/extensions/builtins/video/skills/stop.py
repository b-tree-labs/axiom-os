# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``video.stop``: clear a source's started mark and bring its capture down."""

from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

from .. import control
from ._common import command, need_name, source_name, state_dir


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    name = source_name(params)
    if not name:
        return need_name("stop")
    outcome = control.stop(name, state_dir=state_dir(ctx))
    if not outcome["was_running"]:
        return SkillResult(
            value=outcome, actions_taken=[f"{name} was not being captured; it is not started now"]
        )
    if outcome["stopped"]:
        how = " after SIGTERM" if outcome["signalled"] else ""
        return SkillResult(
            value=outcome, actions_taken=[f"stopped capturing {name} (pid {outcome['pid']}){how}"]
        )
    return SkillResult(
        ok=False,
        value=outcome,
        errors=[
            f"pid {outcome['pid']} still holds {name}. Fix: it is not a process `{command()} start` "
            f"spawned for {name} alone, so it was not signalled; stop that service, or wait for it to let go"
        ],
    )


__all__ = ["run"]
