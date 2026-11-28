# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``video.start``: mark a source started and bring its capture up."""

from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

from .. import control
from ..sources import VideoSourceError
from ._common import need_name, refuse, source_name, state_dir


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    name = source_name(params)
    if not name:
        return need_name("start")
    try:
        outcome = control.start(name, state_dir=state_dir(ctx))
    except VideoSourceError as exc:
        return refuse(str(exc), exc.fix)
    if outcome["already_running"]:
        action = f"{name} is already captured by pid {outcome['pid']}; nothing to start"
    else:
        action = f"started capturing {name} (pid {outcome['pid']})"
    return SkillResult(value=outcome, actions_taken=[action])


__all__ = ["run"]
