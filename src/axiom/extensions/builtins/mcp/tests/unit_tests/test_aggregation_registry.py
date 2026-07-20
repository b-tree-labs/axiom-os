# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Registry-driven tool surface integrated into AggregationRegistry.build() (ADR-073).

Capabilities that opt into surfaces=["mcp"] become MCP tools through build(),
sorting after platform primitives and winning over a colliding legacy manifest
tool — while leaving the aggregation invariants (platform-first, no-shadow,
deterministic hash) intact.
"""

from __future__ import annotations

import asyncio
import logging
import warnings

import pytest

from axiom.extensions.builtins.mcp.aggregation import AggregationRegistry
from axiom.infra.skills import SkillContext, SkillRegistry, SkillResult, SkillSpec


def _reg_with(*specs: SkillSpec) -> SkillRegistry:
    r = SkillRegistry()
    for s in specs:
        r.register_skill(s)
    return r


def _ctx_factory(reg, tmp_path):
    return lambda: SkillContext(registry=reg, state_dir=tmp_path, logger=logging.getLogger("t"))


def _agg(reg, tmp_path, extensions=None):
    return AggregationRegistry(
        extensions=extensions or [],
        registry=reg,
        ctx_factory=_ctx_factory(reg, tmp_path),
    )


def test_mcp_surface_capability_becomes_tool(tmp_axiom_home, tmp_path):
    reg = _reg_with(
        SkillSpec(
            name="alpha.ping",
            fn=lambda p, c: SkillResult(ok=True, value="pong"),
            description="ping the alpha service",
            surfaces=("mcp",),
            side_effects=False,
        )
    )
    surface = _agg(reg, tmp_path).build()
    assert "axiom_alpha__ping" in [t.name for t in surface.tools]
    assert surface.handler_source("axiom_alpha__ping") == "registry"
    # platform invariant unchanged: platform source still first.
    assert surface.sources[0].kind == "platform"
    assert surface.handler_source("axiom_memory__compose") == "platform"


def test_capability_without_mcp_surface_is_inert(tmp_axiom_home, tmp_path):
    base = AggregationRegistry(extensions=[]).build()
    reg = _reg_with(
        SkillSpec(name="alpha.ping", fn=lambda p, c: SkillResult(ok=True), surfaces=("cli",)),
        SkillSpec(name="beta.scan", fn=lambda p, c: SkillResult(ok=True)),  # undeclared
    )
    surface = _agg(reg, tmp_path).build()
    # No opt-in → no registry tools → surface identical to the empty baseline.
    assert surface.content_hash == base.content_hash


def test_registry_dispatch_invokes_capability(tmp_axiom_home, tmp_path):
    reg = _reg_with(
        SkillSpec(
            name="alpha.ping",
            fn=lambda p, c: SkillResult(ok=True, value={"echo": p.get("msg")}),
            surfaces=("mcp",),
        )
    )
    surface = _agg(reg, tmp_path).build()
    result = asyncio.run(surface.dispatch["axiom_alpha__ping"]({"msg": "hi"}))
    assert result["ok"] is True and result["value"] == {"echo": "hi"}


def test_registry_wins_over_colliding_manifest_tool(make_extension, tmp_axiom_home, tmp_path):
    # Registry capability alpha.ping → axiom_alpha__ping.
    reg = _reg_with(
        SkillSpec(
            name="alpha.ping",
            fn=lambda p, c: SkillResult(ok=True, value="from-registry"),
            surfaces=("mcp",),
        )
    )
    # Manifest ext "alpha" (default prefix axiom_alpha) tool "ping" → same mcp_name.
    ext = make_extension(
        "alpha",
        '''
[extension]
name = "alpha"
version = "0.0.1"
description = "ext alpha"
owner = "axiom-tests"
aeos_version = "0.1.0"

[extension.mcp]
enabled = true

[[extension.provides]]
kind = "tool"
name = "ping"
description = "manifest ping"
entry = "axiom.extensions.builtins.mcp.platform_primitives:_rag_retrieve"

[[extension.mcp.tool]]
name = "ping"
''',
    )
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        surface = _agg(reg, tmp_path, extensions=[ext]).build()
    # Exactly one axiom_alpha__ping, owned by the registry (manifest dropped).
    assert [t.name for t in surface.tools].count("axiom_alpha__ping") == 1
    assert surface.handler_source("axiom_alpha__ping") == "registry"
    assert any("already provided" in str(w.message) for w in caught)


def test_registry_cannot_shadow_platform(tmp_axiom_home, tmp_path):
    # A capability mapping onto a platform tool name is dropped (platform wins).
    reg = _reg_with(
        SkillSpec(
            name="memory.compose",  # → axiom_memory__compose (a platform tool)
            fn=lambda p, c: SkillResult(ok=True, value="hijack"),
            surfaces=("mcp",),
        )
    )
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        surface = _agg(reg, tmp_path).build()
    assert surface.handler_source("axiom_memory__compose") == "platform"
    assert any("platform" in str(w.message).lower() for w in caught)


@pytest.mark.parametrize("name,expected", [("alpha.ping", "axiom_alpha__ping")])
def test_names_match_existing_convention(name, expected, tmp_axiom_home, tmp_path):
    reg = _reg_with(SkillSpec(name=name, fn=lambda p, c: SkillResult(ok=True), surfaces=("mcp",)))
    surface = _agg(reg, tmp_path).build()
    assert expected in [t.name for t in surface.tools]
