# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``video.install``: register capture --all as a unit the OS starts at boot, and start it."""

from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

from .. import boot
from ..sources import VideoSourceError
from ._common import refuse, state_dir


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    try:
        outcome = boot.install(state_dir(ctx))
    except VideoSourceError as exc:
        return refuse(str(exc), exc.fix)
    actions = [
        f"registered {outcome['name']} with {outcome['provider']}, capturing every started source"
    ]
    actions.append(
        "started it" if outcome["started"] else f"{outcome['provider']} did not start it yet"
    )
    if not outcome["survives_reboot"]:
        actions.append(f"it will not outlive the login: {outcome['fix']}")
    return SkillResult(value=outcome, actions_taken=actions)


__all__ = ["run"]
