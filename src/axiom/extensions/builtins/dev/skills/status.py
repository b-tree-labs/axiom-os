# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``dev.status`` — whether this checkout's node is up, and where."""

from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

from .. import node
from . import resolve_lane


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    lane, errors = resolve_lane(params)
    if errors:
        return SkillResult(ok=False, errors=errors)
    home = node.home_for(ctx.state_dir, lane["name"])
    record = node.read_record(home)
    if record is None:
        return SkillResult(value={"lane": lane["name"], "state": "down"}, actions_taken=["down"])
    running = node.alive(int(record["pid"]))
    answering = running and node.answers(record["url"])
    state = "up" if answering else ("not answering" if running else "exited")
    return SkillResult(
        value={"lane": lane["name"], "state": state, **record},
        actions_taken=[f"{state}: {record['url']}"],
    )
