# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``dev.down`` — stop this checkout's node. Keeps its accounts and log."""

from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

from .. import node
from . import resolve_lane


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    lane, errors = resolve_lane(params)
    if errors:
        return SkillResult(ok=False, errors=errors)
    out = node.stop(node.home_for(ctx.state_dir, lane["name"]))
    said = f"stopped pid {out['pid']}" if out["stopped"] else out["reason"]
    return SkillResult(value={"lane": lane["name"], **out}, actions_taken=[said])
