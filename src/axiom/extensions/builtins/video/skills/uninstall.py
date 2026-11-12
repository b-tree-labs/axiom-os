# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``video.uninstall``: stop and remove the boot unit; the started marks stay."""

from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

from .. import boot
from ._common import state_dir


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    outcome = boot.uninstall(state_dir(ctx))
    actions = [f"stopped and removed {outcome['name']} from {outcome['provider']}"]
    if outcome["note"]:
        actions.append(outcome["note"])
    if not outcome["uninstalled"]:
        return SkillResult(
            ok=False,
            value=outcome,
            errors=[
                f"{outcome['provider']} did not remove {outcome['name']}. Fix: retry, then check its user manager's log"
            ],
        )
    return SkillResult(value=outcome, actions_taken=actions)


__all__ = ["run"]
