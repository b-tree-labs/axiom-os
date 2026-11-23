# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""analytics-on-MCP (ADR-073): the capability declares surfaces=["mcp"] and is
projected onto the MCP tool surface by the shared registry projector — the first
builtin adopter of the registry MCP path."""
from __future__ import annotations

import asyncio
import logging

from axiom.extensions.builtins.analytics.skills import bind
from axiom.extensions.builtins.mcp.skill_tools import (
    is_mcp_exposed,
    mcp_name_to_capability,
    skill_tool_contribution,
)
from axiom.infra.skills import SkillContext, SkillRegistry


def _registry():
    r = SkillRegistry()
    bind(r)
    return r


def test_spec_declares_mcp_surface():
    r = _registry()
    spec = r.specs()["analytics.compute"]
    assert is_mcp_exposed(spec)
    assert set(spec.surfaces) >= {"cli", "mcp", "agent_tool"}
    assert spec.idempotent is True and spec.side_effects is False


def test_projected_onto_mcp_tool_surface(tmp_path):
    r = _registry()
    def ctx():
        return SkillContext(registry=r, state_dir=tmp_path, logger=logging.getLogger("t"))
    contrib = skill_tool_contribution(r, ctx_factory=ctx)
    caps = {mcp_name_to_capability(t.name) for t in contrib.tools}
    assert "analytics.compute" in caps, f"analytics not projected onto MCP; got {caps}"


def test_mcp_dispatch_computes(tmp_path):
    r = _registry()
    def ctx():
        return SkillContext(registry=r, state_dir=tmp_path, logger=logging.getLogger("t"))
    contrib = skill_tool_contribution(r, ctx_factory=ctx)
    name = next(t.name for t in contrib.tools if mcp_name_to_capability(t.name) == "analytics.compute")
    result = asyncio.run(contrib.dispatch[name]({"op": "mean", "series": [2.0, 4.0, 6.0]}))
    # the dispatched result carries the analytics payload (mean of [2,4,6] = 4).
    assert "4" in str(result)


def test_analytics_on_the_node_runtime_mcp_surface():
    # The real path: the MCP server binds discovered extensions' skills into the
    # registry, so from_node().build() must actually surface analytics (not just
    # an explicit-bind test). This is what was dormant before the aggregation fix.
    from axiom.extensions.builtins.mcp.aggregation import AggregationRegistry

    names = {t.name for t in AggregationRegistry.from_node().build().tools}
    assert "axiom_analytics__compute" in names, sorted(n for n in names if "analytics" in n)
