# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Analytics skill registration (ADR-056 / ADR-072 / ADR-073).

Registers the deterministic series-math capability and DECLARES its surfaces so it
reaches every projection — CLI (`axi analytics`), the agent tool loop, and **MCP**
(so Claude Code / Cursor / axi-chat-over-MCP can call it too, not just native AXI).
The loader imports ``<ext>.skills`` and calls ``bind_default()``.
"""
from __future__ import annotations

from axiom.infra.skills import SkillRegistry, SkillSpec, default_registry

from .compute import run

_SPEC = SkillSpec(
    name="analytics.compute",
    fn=run,
    description=(
        "Deterministic math over a numeric series (descriptive stats, regression/fit, "
        "calculus on samples). Call this instead of computing an average/total/slope/"
        "integral yourself; the result is provenance-stamped."
    ),
    inputs={
        "op": "sum|mean|min|max|std|var|median|percentile|range|count|linregress|"
        "polyfit|moving_average|rate_of_change|correlation|derivative|integral|cumsum|interpolate",
        "series": "list of numbers, list of row-objects, or JSON/CSV/table text",
        "column": "column/key to use when series is rows (optional)",
        "params": "op params, e.g. {window:7}, {degree:2}, {q:95} (optional)",
        "source": "provenance: the tool/series the data came from (optional)",
    },
    # Pure compute: no side effects, idempotent -> a READ tool on every surface.
    side_effects=False,
    idempotent=True,
    # Bounded exposure (ADR-072 §4.9.4): opt into CLI + agent tool + MCP.
    surfaces=("cli", "mcp", "agent_tool"),
)


def bind(registry: SkillRegistry) -> None:
    if not registry.has(_SPEC.name):
        registry.register_skill(_SPEC)


def bind_default() -> SkillRegistry:
    reg = default_registry()
    bind(reg)
    return reg


__all__ = ["bind", "bind_default", "run"]
