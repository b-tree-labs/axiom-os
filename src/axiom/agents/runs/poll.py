# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``<capability>.status`` and ``<capability>.cancel``: the poll tools (H2).

ADR-232 part 6: a capability that returns a run handle also exposes a status
tool and a cancel tool, registered as ordinary capabilities. Because they go
through :class:`SkillRegistry`, ADR-072 projects them to the CLI, the MCP tool
list and the agent-tool list from this one registration. This is the only path
required to ship; native MCP progress and A2A tasks come later (H3, H4).

Ownership: a run belongs to the principal it acts for. A caller asking about a
run it does not own gets ``run_not_found``, the same answer as for a run that
does not exist, so ids cannot be probed.
"""

from __future__ import annotations

from typing import Any

from axiom.agents.runs.manager import RUN_NOT_FOUND, RunManager
from axiom.infra.skills import SkillContext, SkillRegistry, SkillResult, SkillSpec

SURFACES = ("cli", "mcp", "agent_tool")


def poll_tool_names(capability: str) -> tuple[str, str]:
    return f"{capability}.status", f"{capability}.cancel"


def _refusal(run_id: str) -> SkillResult:
    unmet = {
        "id": RUN_NOT_FOUND,
        "why": f"no run {run_id!r} for this caller",
        "fix": "check the run id",
    }
    return SkillResult(ok=False, errors=[unmet["why"]], unmet=[unmet])


def register_poll_tools(registry: SkillRegistry, capability: str, manager: RunManager) -> None:
    """Register ``<capability>.status`` and ``<capability>.cancel`` on ``registry``."""

    def owned(run_id: str, ctx: SkillContext):
        handle = manager.get(run_id) if run_id else None
        if handle is None or handle.principal != ctx.principal.handle:
            return None
        return handle

    def status(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
        run_id = str(params.get("run_id") or "")
        handle = owned(run_id, ctx)
        if handle is None:
            return _refusal(run_id)
        return SkillResult(ok=True, value=handle.to_dict())

    def cancel(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
        run_id = str(params.get("run_id") or "")
        if owned(run_id, ctx) is None:
            return _refusal(run_id)
        outcome = manager.cancel(run_id)
        if outcome.handle is None:
            return SkillResult(
                ok=False,
                errors=[u["why"] for u in outcome.unmet],
                unmet=list(outcome.unmet),
                value=manager.get(run_id).to_dict() if manager.get(run_id) else None,
            )
        return SkillResult(
            ok=True,
            value=outcome.handle.to_dict(),
            actions_taken=[f"cancelled {run_id}"],
        )

    status_name, cancel_name = poll_tool_names(capability)
    registry.register_skill(
        SkillSpec(
            name=status_name,
            fn=status,
            description=f"State, budget drawn and result reference of a {capability} run.",
            inputs={"run_id": "str!"},
            side_effects=False,
            idempotent=True,
            risk="read",
            surfaces=SURFACES,
        ),
        mutating=False,
    )
    registry.register_skill(
        SkillSpec(
            name=cancel_name,
            fn=cancel,
            description=f"Cancel an unfinished {capability} run at its next step boundary.",
            inputs={"run_id": "str!"},
            side_effects=True,
            idempotent=True,
            risk="reversible-write",
            surfaces=SURFACES,
        ),
    )


__all__ = ["SURFACES", "poll_tool_names", "register_poll_tools"]
