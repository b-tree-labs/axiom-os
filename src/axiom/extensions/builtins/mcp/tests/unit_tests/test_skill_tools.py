# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Registry-driven MCP tool surface (ADR-073).

MCP tools for extension capabilities derive from the SkillRegistry via the
shared projector — names, schema, side-effects, and dispatch all come from the
capability, not a parallel manifest convention.
"""

from __future__ import annotations

import asyncio
import logging

from axiom.extensions.builtins.mcp.skill_tools import (
    MCP_PREFIX,
    is_mcp_exposed,
    mcp_name_to_capability,
    mcp_tool_name,
    skill_tool_contribution,
)
from axiom.infra.skills import SkillContext, SkillRegistry, SkillResult, SkillSpec


def _registry() -> SkillRegistry:
    r = SkillRegistry()
    r.register_skill(
        SkillSpec(
            name="press.draft",
            fn=lambda p, c: SkillResult(ok=True, value={"drafted": p.get("source")}),
            description="Render a document draft locally.",
            inputs={"source": "Path", "copies": "int"},
            side_effects=True,
            surfaces=("cli", "mcp", "agent_tool"),
        )
    )
    r.register_skill(
        SkillSpec(
            name="scan.status",
            fn=lambda p, c: SkillResult(ok=True, value={"signals": 3}),
            description="Show signal counts.",
            side_effects=False,
            idempotent=True,
            surfaces=("cli", "mcp"),
        )
    )
    # Not MCP-exposed (no "mcp" in surfaces) — must be excluded.
    r.register_skill(
        SkillSpec(
            name="press.publish",
            fn=lambda p, c: SkillResult(ok=True),
            description="Publish end-to-end.",
            surfaces=("cli",),
        )
    )
    # Undeclared surfaces — also excluded (bounded exposure, not opt-out).
    r.register_skill(SkillSpec(name="data.reindex", fn=lambda p, c: SkillResult(ok=True)))
    return r


def _ctx_factory(reg, tmp_path):
    return lambda: SkillContext(registry=reg, state_dir=tmp_path, logger=logging.getLogger("t"))


# --- naming: axiom_ prefix + projector round-trip ------------------------- #
def test_mcp_name_preserves_existing_convention():
    assert mcp_tool_name("classroom.enroll") == "axiom_classroom__enroll"
    assert mcp_tool_name("press.draft") == "axiom_press__draft"


def test_mcp_name_roundtrip_recovers_capability():
    for cap in ("press.draft", "classroom.enroll", "data.reindex"):
        assert mcp_name_to_capability(mcp_tool_name(cap)) == cap


def test_mcp_prefix_constant():
    assert MCP_PREFIX == "axiom_"


# --- exposure gate -------------------------------------------------------- #
def test_is_mcp_exposed_requires_mcp_in_surfaces():
    specs = _registry().specs()
    assert is_mcp_exposed(specs["press.draft"]) is True
    assert is_mcp_exposed(specs["scan.status"]) is True
    assert is_mcp_exposed(specs["press.publish"]) is False  # cli only
    assert is_mcp_exposed(specs["data.reindex"]) is False  # undeclared


# --- contribution: tools + dispatch --------------------------------------- #
def test_only_mcp_surface_capabilities_become_tools(tmp_path):
    reg = _registry()
    contrib = skill_tool_contribution(reg, ctx_factory=_ctx_factory(reg, tmp_path))
    names = {t.name for t in contrib.tools}
    assert names == {"axiom_press__draft", "axiom_scan__status"}


def test_tool_schema_from_projector(tmp_path):
    reg = _registry()
    contrib = skill_tool_contribution(reg, ctx_factory=_ctx_factory(reg, tmp_path))
    tool = {t.name: t for t in contrib.tools}["axiom_press__draft"]
    assert tool.inputSchema["properties"]["source"]["type"] == "string"  # Path → string
    assert tool.inputSchema["properties"]["copies"]["type"] == "integer"


def test_tool_annotations_from_capability_side_effects(tmp_path):
    reg = _registry()
    tools = {t.name: t for t in skill_tool_contribution(reg, ctx_factory=_ctx_factory(reg, tmp_path)).tools}
    assert tools["axiom_scan__status"].annotations.readOnlyHint is True  # side_effects=False
    assert tools["axiom_scan__status"].annotations.idempotentHint is True
    assert tools["axiom_press__draft"].annotations.readOnlyHint is False  # side_effects=True


def test_dispatch_invokes_the_capability(tmp_path):
    reg = _registry()
    contrib = skill_tool_contribution(reg, ctx_factory=_ctx_factory(reg, tmp_path))
    handler = contrib.dispatch["axiom_press__draft"]
    result = asyncio.run(handler({"source": "/tmp/x.md"}))
    assert result["ok"] is True
    assert result["value"] == {"drafted": "/tmp/x.md"}


def test_dispatch_surfaces_skill_failure(tmp_path):
    reg = SkillRegistry()
    reg.register_skill(
        SkillSpec(
            name="x.fail",
            fn=lambda p, c: SkillResult(ok=False, errors=["boom"]),
            surfaces=("mcp",),
        )
    )
    contrib = skill_tool_contribution(reg, ctx_factory=_ctx_factory(reg, tmp_path))
    result = asyncio.run(contrib.dispatch["axiom_x__fail"]({}))
    assert result["ok"] is False
    assert "boom" in result["errors"]
