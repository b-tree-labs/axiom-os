# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Who can be asked."""

from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillContext, SkillResult


def roster(params: dict[str, Any] | None = None, ctx: SkillContext | None = None, **_: Any) -> SkillResult:
    """List the agents ``agents.ask`` can address.

    ``SkillRegistry.invoke`` calls every skill as ``fn(params, ctx)``. A roster
    that only accepted keywords never ran from MCP.
    """
    del params, ctx
    from axiom.extensions.builtins.connect.agent_router import discover_agents

    return SkillResult(ok=True, value={"agents": sorted(discover_agents())})


__all__ = ["roster"]
