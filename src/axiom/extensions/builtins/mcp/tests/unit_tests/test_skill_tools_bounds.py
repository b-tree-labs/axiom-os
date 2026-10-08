# Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""MCP results fit a context window, and undeclared arguments are refused.

Measured 2026-10-02: a series call returned 327 KB over MCP and a colleague's
harness refused the 459,096-character message. And the MCP layer forwarded
arguments verbatim, so a caller could pass ``dsn=`` into a verb whose schema
never declared one — the chat surface has refused those since ADR-072; this
makes MCP behave the same (ADR-157 Phases 0.2 and 1.5).
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from axiom.extensions.builtins.mcp.skill_tools import skill_tool_contribution
from axiom.infra.skills import SkillContext, SkillRegistry, SkillResult, SkillSpec


def _registry_with(fn, *, inputs, cost_class=None):
    reg = SkillRegistry()
    reg.register_skill(
        SkillSpec(
            name="data.series",
            fn=fn,
            description="test verb",
            inputs=inputs,
            idempotent=True,
            side_effects=False,
            surfaces=("cli", "mcp", "agent_tool"),
            cost_class=cost_class,
        )
    )
    return reg


def _ctx_factory(reg, tmp_path):
    def make():
        return SkillContext(
            registry=reg, state_dir=Path(tmp_path), logger=logging.getLogger("t")
        )

    return make


def _dispatch(reg, tmp_path):
    contribution = skill_tool_contribution(reg, ctx_factory=_ctx_factory(reg, tmp_path))
    (name,) = contribution.dispatch
    return contribution.dispatch[name]


def test_an_undeclared_argument_is_refused_not_forwarded(tmp_path):
    seen = {}

    def fn(params, ctx):
        seen.update(params)
        return SkillResult(ok=True, value={"data": None})

    handler = _dispatch(
        _registry_with(fn, inputs={"table": "str!"}, cost_class="series"), tmp_path
    )
    out = asyncio.run(handler({"table": "t", "dsn": "postgres://evil"}))
    assert out["ok"] is False
    assert "dsn" in out["errors"][0]
    assert not seen, "the skill must never see the refused call"


def test_a_declared_argument_still_flows(tmp_path):
    def fn(params, ctx):
        return SkillResult(ok=True, value={"echo": params["table"]})

    handler = _dispatch(
        _registry_with(fn, inputs={"table": "str!"}, cost_class="series"), tmp_path
    )
    out = asyncio.run(handler({"table": "t"}))
    assert out["ok"] is True and out["value"]["echo"] == "t"


def test_an_oversized_result_is_reduced_to_the_agent_byte_budget(tmp_path):
    def fn(params, ctx):
        return SkillResult(
            ok=True,
            value={"data": {"series": [{"t": i, "value": float(i)} for i in range(50_000)]}},
        )

    handler = _dispatch(
        _registry_with(fn, inputs={"table": "str"}, cost_class="series"), tmp_path
    )
    out = asyncio.run(handler({}))
    assert out["ok"] is True
    raw = json.dumps(out)
    assert len(raw) <= 256_000, "the agent series byte budget holds"
    pts = out["value"]["data"]["series"]
    assert pts[0]["t"] == 0 and pts[-1]["t"] == 49_999, (
        "reduction samples the whole span, never the oldest slice"
    )
    assert any("reduced" in n for n in out.get("notes", []))


def test_a_small_result_passes_byte_bounding_untouched(tmp_path):
    def fn(params, ctx):
        return SkillResult(ok=True, value={"data": {"value": 1.0}})

    handler = _dispatch(
        _registry_with(fn, inputs={}, cost_class="lookup"), tmp_path
    )
    out = asyncio.run(handler({}))
    assert out["ok"] is True
    assert "notes" not in out
    assert out["value"] == {"data": {"value": 1.0}}
