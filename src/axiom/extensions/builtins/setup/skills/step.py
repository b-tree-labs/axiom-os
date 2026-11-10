# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""One skill per setup step (``setup.vault``, ``setup.key`` ...), so each is drivable alone.

``action`` is ``check`` (read only, the default), ``plan`` (what apply would do) or
``apply`` (do it, then check). ``apply`` is the caller's decision: this skill does
not ask, because the caller (the ``setup`` walk, or a chat that asked the person)
already did.
"""

from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

from ..environment import resolve_env
from ..steps import Step


def skill_for(step: Step):
    def run(params: dict[str, Any], ctx: SkillContext | None = None) -> SkillResult:
        action = str(params.get("action") or "check")
        env = resolve_env(params)
        if action == "check":
            line = step.check(env)
        elif action == "plan":
            line = step.check(env)
            plan = None if line.green else step.plan(env)
            return SkillResult(ok=True, value={**line.as_dict(), "plan": plan})
        elif action == "apply":
            line = step.apply(env)
        else:
            return SkillResult(ok=False, errors=[f"action must be check, plan or apply, not {action!r}"])
        return SkillResult(ok=line.green, value=line.as_dict(),
                           errors=[] if line.green else [line.status])

    run.__name__ = f"setup_{step.name}"
    run.__doc__ = f"The `{step.name}` setup step: check, plan or apply."
    return run


__all__ = ["skill_for"]
