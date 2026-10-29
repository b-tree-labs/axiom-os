# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The chaining layer as an MCP client sees it (ADR-185).

Real registry, real projector, real handlers. Covered: the additive versioned
``chain`` key; the reserved ``_chain`` argument (never an "undeclared
parameter", recorded in the audit); a destructive tool's ``plan_hash``; tool
``_meta`` hints that are absent when nothing is declared; recipes published as
prompts only when every step is a real tool on the surface; and the handshake.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from axiom.extensions.builtins.mcp.aggregation import AggregationRegistry
from axiom.extensions.builtins.mcp.skill_tools import skill_tool_contribution
from axiom.extensions.builtins.recipe import skills as recipe_skills
from axiom.extensions.builtins.secrets import skills as secrets_skills
from axiom.extensions.builtins.vault import skills as vault_skills
from axiom.infra import audit_trail
from axiom.infra.orchestrator.approval import ApprovalGate
from axiom.infra.orchestrator.approval_store import FileActionStore
from axiom.infra.skill_declarations import Precondition, Ref
from axiom.infra.skills import SkillContext, SkillRegistry, SkillResult, SkillSpec

MCP = ("cli", "mcp", "agent_tool")


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    from axiom.infra import authority

    monkeypatch.setenv("AXI_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("AXIOM_AUTHORITY_RECEIPTS", "off")
    monkeypatch.delenv("AXIOM_DB_URL", raising=False)
    monkeypatch.setattr(authority, "_default_ctx", authority.build_tool_context(session_factory=None))
    audit_trail._reset_for_tests()


def _build(tmp_path):
    reg = SkillRegistry()
    ran = []
    reg.register_skill(SkillSpec(
        name="t.plain", fn=lambda p, c: SkillResult(value={"x": 1}), description="plain", surfaces=MCP,
        side_effects=False, inputs={"q": "str"}))
    reg.register_skill(SkillSpec(
        name="t.make", fn=lambda p, c: (ran.append("make"), SkillResult(value={"thing_id": "t1"}))[1],
        description="make", surfaces=MCP, side_effects=True, risk="reversible-write", inputs={"q": "str"},
        requires=(Precondition("go", "needs go", check=lambda p, c: bool(p.get("q")), fix="pass q"),),
        produces=(Ref("thing_id", "pass as thing to t.drop"),)))
    reg.register_skill(SkillSpec(
        name="t.drop", fn=lambda p, c: (ran.append("drop"), SkillResult(value={"gone": p["thing"]}))[1],
        description="drop", surfaces=MCP, side_effects=True, risk="destructive", inputs={"thing": "str"},
        requires=()))
    ctx = SkillContext(registry=reg, state_dir=tmp_path, logger=logging.getLogger("t"), user_prompt=None)
    contrib = skill_tool_contribution(reg, ctx_factory=lambda: ctx)
    return reg, ctx, contrib, ran


def call(contrib, tool, args):
    return asyncio.run(contrib.dispatch[tool](args))


def test_an_undeclared_tool_lists_and_answers_exactly_as_before(tmp_path):
    _, _, contrib, _ = _build(tmp_path)
    plain = next(t for t in contrib.tools if t.name == "axiom_t__plain")
    assert plain.meta is None  # no _meta at all
    out = call(contrib, "axiom_t__plain", {"q": "a"})
    assert set(out) == {"ok", "value", "errors", "actions_taken"}  # no chain key


def test_declared_tools_carry_compact_meta_hints(tmp_path):
    _, _, contrib, _ = _build(tmp_path)
    make = next(t for t in contrib.tools if t.name == "axiom_t__make")
    hints = make.meta["ai.axiom/chain"]
    assert hints["requires"] == [{"id": "go", "fix": "pass q"}]
    assert [r["kind"] for r in hints["produces"]] == ["thing_id"]
    assert hints["risk"] == "reversible-write"


def test_an_unmet_precondition_comes_back_structured_over_mcp(tmp_path):
    _, _, contrib, ran = _build(tmp_path)
    out = call(contrib, "axiom_t__make", {})
    assert out["ok"] is False and ran == []
    assert out["chain"] == {"v": 1, "unmet": [{"id": "go", "why": "needs go", "fix": "pass q"}]}


def test_produced_refs_ride_the_versioned_key_and_existing_fields_are_untouched(tmp_path):
    _, _, contrib, _ = _build(tmp_path)
    out = call(contrib, "axiom_t__make", {"q": "a"})
    assert out["ok"] is True and out["value"] == {"thing_id": "t1"}
    assert out["chain"]["v"] == 1
    assert out["chain"]["produced"] == [{"kind": "thing_id", "value": "t1", "how_to_use": "pass as thing to t.drop"}]


def test_the_reserved_chain_argument_is_bookkeeping_not_an_undeclared_parameter(tmp_path):
    _, ctx, contrib, _ = _build(tmp_path)
    out = call(contrib, "axiom_t__make", {"q": "a", "_chain": {"chain_id": "c-9"}})
    assert out["ok"] is True  # not refused as an undeclared parameter
    assert out["chain"]["chain_id"] == "c-9" and out["chain"]["step_id"]
    rec = audit_trail.read_chain(audit_trail.audit_path(ctx.state_dir))[-1]
    assert rec["chain_id"] == "c-9" and rec["step_id"] == out["chain"]["step_id"]
    stray = call(contrib, "axiom_t__make", {"q": "a", "bogus": 1})
    assert stray["ok"] is False and "bogus" in stray["errors"][0]


def test_a_destructive_tool_advertises_plan_hash_and_refuses_without_one(tmp_path):
    _, ctx, contrib, ran = _build(tmp_path)
    drop = next(t for t in contrib.tools if t.name == "axiom_t__drop")
    assert "plan_hash" in drop.input_schema["properties"]
    first = call(contrib, "axiom_t__drop", {"thing": "t1"})
    assert first["ok"] is False and ran == []
    assert first["chain"]["unmet"][0]["id"] == "approved-plan"
    h = first["value"]["plan_hash"]
    ApprovalGate(FileActionStore(ctx.state_dir / "orchestrator" / "approvals.json")).approve(
        first["value"]["held_action_id"], decided_by="@person:test")
    done = call(contrib, "axiom_t__drop", {"thing": "t1", "plan_hash": h})  # plan_hash is not "undeclared"
    assert done["ok"] is True and ran == ["drop"]


def test_the_envelope_is_bounded(tmp_path):
    from axiom.infra.skill_chain import MAX_NEXT, MAX_PRODUCED

    assert MAX_NEXT <= 5 and MAX_PRODUCED <= 6


# ---------------------------------------------------------------- recipes as prompts


def _surface(tmp_path, *binders):
    reg = SkillRegistry()
    for b in binders:
        b.bind(reg)
    ctx = SkillContext(registry=reg, state_dir=tmp_path, logger=logging.getLogger("t"), user_prompt=None)
    return AggregationRegistry(extensions=[], registry=reg, ctx_factory=lambda: ctx).build()


def test_a_recipe_is_published_only_when_every_step_is_a_real_tool_here(tmp_path):
    full = _surface(tmp_path, recipe_skills, secrets_skills, vault_skills)
    names = [p.name for p in full.prompts]
    assert names == ["axiom_recipe__credential_health_review"]
    # nothing bound: no recipe has its steps, so none is advertised
    assert [p.name for p in _surface(tmp_path, recipe_skills).prompts] == []


def test_a_recipe_prompt_is_served_through_both_doors(tmp_path):
    surface = _surface(tmp_path, recipe_skills, secrets_skills, vault_skills)
    prompt = surface.prompts[0]
    assert "not a harness built-in command" in prompt.description
    body = asyncio.run(surface.dispatch["axiom_prompts__get"]({"name": prompt.name}))
    assert body["ok"] and "`axiom_vault__audit`" in body["content"] and "_chain" in body["content"]
    listed = asyncio.run(surface.dispatch["axiom_prompts__list"]({}))
    assert prompt.name in [p["name"] for p in listed["prompts"]]


def test_the_handshake_names_the_recipe_and_stays_in_budget(tmp_path):
    from axiom.extensions.builtins.mcp.server import instructions_with_discovery
    from axiom.memory.rendering import CAPABILITY_BLOCK_MAX_BYTES

    surface = _surface(tmp_path, recipe_skills, secrets_skills, vault_skills)
    text = instructions_with_discovery(surface.tools, state_dir=tmp_path)
    assert "follow recipe `axiom_recipe__credential_health_review`" in text
    block = text[text.index("<!-- axiom:capabilities:begin -->"):]
    assert len(block.encode()) <= CAPABILITY_BLOCK_MAX_BYTES
    assert "A tool result may carry a `chain` key" in text
