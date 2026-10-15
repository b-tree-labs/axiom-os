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
from axiom.infra.hooks import HookBus, HookSpec, allow, deny, set_default_hookbus
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
    assert tool.input_schema["properties"]["source"]["type"] == "string"  # Path → string
    assert tool.input_schema["properties"]["copies"]["type"] == "integer"


def test_tool_annotations_from_capability_side_effects(tmp_path):
    reg = _registry()
    tools = {t.name: t for t in skill_tool_contribution(reg, ctx_factory=_ctx_factory(reg, tmp_path)).tools}
    assert tools["axiom_scan__status"].annotations.read_only_hint is True  # side_effects=False
    assert tools["axiom_scan__status"].annotations.idempotent_hint is True
    assert tools["axiom_press__draft"].annotations.read_only_hint is False  # side_effects=True


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


# --- the dispatch goes through the tool gateway (P5, step 2) -------------- #
def test_dispatch_goes_through_the_gateway_so_a_deny_hook_stops_it(tmp_path, monkeypatch):
    """A ``tool.pre_invoke`` deny refuses an MCP call before the skill body runs.

    The MCP handler used to call ``SkillRegistry.invoke`` directly and so never
    reached the hook chain. Routing it through ``invoke_capability`` means one
    site rule on ``tool://<capability>`` covers the MCP surface too.
    """
    monkeypatch.setenv("AXI_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("AXIOM_AUTHORITY_RECEIPTS", "off")

    ran: list[dict] = []

    def body(params, ctx):
        ran.append(dict(params))
        return SkillResult(ok=True, value="should not happen")

    reg = SkillRegistry()
    reg.register_skill(SkillSpec(name="press.draft", fn=body, surfaces=("mcp",)))

    bus = HookBus()
    bus.register(
        HookSpec(
            event="tool.pre_invoke",
            entry=lambda ctx: deny(reason="site policy refuses press.draft"),
            priority=10,
            fail_mode="abort",
            source="test",
        )
    )
    set_default_hookbus(bus)
    try:
        contrib = skill_tool_contribution(reg, ctx_factory=_ctx_factory(reg, tmp_path))
        result = asyncio.run(contrib.dispatch["axiom_press__draft"]({"source": "x.md"}))
    finally:
        set_default_hookbus(None)

    assert result["ok"] is False
    assert ran == []
    assert any("site policy refuses press.draft" in e for e in result["errors"])


def test_the_gateway_sees_the_capability_name_not_the_mcp_tool_name(tmp_path, monkeypatch):
    """One identity: the mangled ``axiom_press__draft`` stops at the transport."""
    monkeypatch.setenv("AXI_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("AXIOM_AUTHORITY_RECEIPTS", "off")

    seen: list[dict] = []
    reg = _registry()
    bus = HookBus()

    def hook(ctx):
        seen.append(dict(ctx.payload))
        return allow()

    bus.register(
        HookSpec(event="tool.pre_invoke", entry=hook, priority=10, fail_mode="abort", source="test")
    )
    set_default_hookbus(bus)
    try:
        contrib = skill_tool_contribution(reg, ctx_factory=_ctx_factory(reg, tmp_path))
        asyncio.run(contrib.dispatch["axiom_press__draft"]({"source": "x.md"}))
    finally:
        set_default_hookbus(None)

    assert seen[0]["tool_name"] == "press.draft"
    assert seen[0]["ext_origin"] == "press"


# --- post-invoke telemetry on the protocol surface ------------------------ #
def test_a_protocol_call_publishes_one_post_invoke_event(tmp_path, monkeypatch):
    """An MCP dispatch passes no bus, so it lands on the process default."""
    from axiom.infra.bus import EventBus, set_default_eventbus

    monkeypatch.setenv("AXI_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("AXIOM_AUTHORITY_RECEIPTS", "off")
    bus = EventBus()
    seen: list[dict] = []
    bus.subscribe("tool.post_invoke", lambda subject, payload: seen.append(payload))
    set_default_eventbus(bus)
    set_default_hookbus(HookBus())
    try:
        reg = _registry()
        contrib = skill_tool_contribution(reg, ctx_factory=_ctx_factory(reg, tmp_path))
        out = asyncio.run(contrib.dispatch["axiom_press__draft"]({"source": "a.md"}))
    finally:
        set_default_eventbus(None)
        set_default_hookbus(None)

    assert out["ok"] is True
    assert len(seen) == 1
    assert seen[0]["tool_name"] == "press.draft"
    assert seen[0]["surface"] == "mcp"


def test_a_protocol_call_publishes_the_capability_name_not_the_tool_name(tmp_path, monkeypatch):
    from axiom.infra.bus import EventBus, set_default_eventbus

    monkeypatch.setenv("AXI_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("AXIOM_AUTHORITY_RECEIPTS", "off")
    bus = EventBus()
    seen: list[dict] = []
    bus.subscribe("tool.post_invoke", lambda subject, payload: seen.append(payload))
    set_default_eventbus(bus)
    set_default_hookbus(HookBus())
    try:
        reg = _registry()
        contrib = skill_tool_contribution(reg, ctx_factory=_ctx_factory(reg, tmp_path))
        asyncio.run(contrib.dispatch["axiom_press__draft"]({"source": "a.md"}))
    finally:
        set_default_eventbus(None)
        set_default_hookbus(None)

    assert seen[0]["tool_name"] != "axiom_press__draft"
    assert seen[0]["tool_name"] == mcp_name_to_capability("axiom_press__draft")


def test_a_protocol_call_publishes_no_arguments_or_results(tmp_path, monkeypatch):
    """A protocol client's arguments are as sensitive as a CLI operator's."""
    import json

    from axiom.infra.bus import EventBus, set_default_eventbus

    monkeypatch.setenv("AXI_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("AXIOM_AUTHORITY_RECEIPTS", "off")
    bus = EventBus()
    seen: list[dict] = []
    bus.subscribe("tool.post_invoke", lambda subject, payload: seen.append(payload))
    set_default_eventbus(bus)
    set_default_hookbus(HookBus())
    try:
        reg = _registry()
        contrib = skill_tool_contribution(reg, ctx_factory=_ctx_factory(reg, tmp_path))
        asyncio.run(contrib.dispatch["axiom_press__draft"]({"source": "glpat-MCPSECRET0000"}))
    finally:
        set_default_eventbus(None)
        set_default_hookbus(None)

    rendered = json.dumps(seen[0], default=str)
    assert "glpat-MCPSECRET0000" not in rendered
    assert "args" not in seen[0]
    assert "result" not in seen[0]
