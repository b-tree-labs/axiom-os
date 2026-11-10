# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""`process.resume`: undo a containment pause for one agent."""

from __future__ import annotations

from typing import Any

from axiom.infra.process_containment import resume_agent
from axiom.infra.skills import SkillContext, SkillResult

from ._common import next_steps, registry_for


def resume(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    agent = str(params.get("agent", "")).strip()
    if not agent:
        return SkillResult(ok=False, errors=["agent name required"])
    res = resume_agent(agent, state_dir=registry_for(ctx).state_dir,
                       execute=bool(params.get("yes", False)))
    steps = next_steps(f"{{cli}} process resume {agent} --yes   re-enable it",
                       "{cli} agents status") if res["action"] == "dry-run" else         next_steps("{cli} agents status             confirm", "{cli} process sweep              check it behaves")
    return SkillResult(ok=res["action"] != "not-paused-by-containment", value={**res, "next": steps},
                       errors=[] if res["action"] != "not-paused-by-containment"
                       else [f"{agent!r} was not paused by containment"])
