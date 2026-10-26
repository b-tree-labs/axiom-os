# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""`process.panic`: pause every agent and stop agent-owned processes."""

from __future__ import annotations

from typing import Any

from axiom.infra.process_containment import panic as run_panic
from axiom.infra.skills import SkillContext, SkillResult

from ._common import next_steps, registry_for


def panic(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    execute = bool(params.get("kill", False))
    out = run_panic(registry_for(ctx), execute=execute)
    if execute:
        steps = next_steps("{cli} process ls               confirm what is left",
                           "{cli} process resume <agent>   turn one agent back on when safe",
                           "{cli} agents status            see the consent set")
    else:
        steps = next_steps("{cli} process panic --kill     do it (agents off, agent processes stopped)",
                           "{cli} process ls               what is registered right now")
    return SkillResult(ok=True, value={**out, "next": steps})
