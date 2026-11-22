# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``data.unregister`` — delete a connector config by name.

CLI maps as ``axi data unregister <name>``.
"""

from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

from ..agents.plinth.skills.register_connector import unregister_connector


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    name = params.get("name")
    if not name:
        return SkillResult(ok=False, errors=["missing required param: name"])

    removed = unregister_connector(name, state_dir=ctx.state_dir)
    if removed:
        return SkillResult(
            ok=True,
            value={"name": name, "removed": True},
            actions_taken=[f"removed connector {name!r}"],
        )
    return SkillResult(
        ok=False,
        errors=[f"no connector named {name!r}"],
    )
