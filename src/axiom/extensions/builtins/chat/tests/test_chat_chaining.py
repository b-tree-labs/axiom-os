# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Chat gets the same chaining layer as MCP (ADR-185): refusals, envelope, plan gate, search fields."""

from __future__ import annotations

import logging

import pytest

from axiom.extensions.builtins.chat import tools as chat_tools
from axiom.extensions.builtins.chat.verb_index import build_verb_index, describe, search_verbs
from axiom.infra import audit_trail
from axiom.infra.skill_declarations import NextStep, Precondition, Ref
from axiom.infra.skills import SkillContext, SkillRegistry, SkillResult, SkillSpec

CHAT = ("cli", "mcp", "agent_tool")


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("AXI_STATE_DIR", str(tmp_path))
    audit_trail._reset_for_tests()
    reg = SkillRegistry()
    ran: list[str] = []
    reg.register_skill(SkillSpec(
        name="t.make", fn=lambda p, c: (ran.append("make"), SkillResult(value={"thing_id": "t1"}))[1],
        description="make a thing", surfaces=CHAT, side_effects=True, risk="reversible-write",
        inputs={"q": "str"}, recipes=("r",),
        requires=(Precondition("go", "needs go", check=lambda p, c: bool(p.get("q")), fix="pass q"),),
        produces=(Ref("thing_id", "pass as thing to t.drop"),),
        next_steps=(NextStep("t.drop", "produced:thing_id", "remove it", {"thing": "thing_id"}),)))
    reg.register_skill(SkillSpec(
        name="t.drop", fn=lambda p, c: (ran.append("drop"), SkillResult(value={}))[1],
        description="drop a thing", surfaces=CHAT, side_effects=True, risk="destructive", inputs={"thing": "str"},
        requires=()))
    ctx = SkillContext(registry=reg, state_dir=tmp_path, logger=logging.getLogger("t"), user_prompt=None)
    monkeypatch.setattr(chat_tools, "_skill_registry", lambda: reg)
    monkeypatch.setattr(chat_tools, "_skill_context", lambda r: ctx)
    return reg, ctx, ran


def test_a_refusal_in_chat_carries_the_exact_fix(world):
    _, _, ran = world
    out = chat_tools._execute_skill_tool("t__make", {})
    assert out["ok"] is False and ran == []
    assert out["chain"]["unmet"] == [{"id": "go", "why": "needs go", "fix": "pass q"}]


def test_a_chat_result_offers_the_next_call_ready_to_make(world):
    out = chat_tools._execute_skill_tool("t__make", {"q": "a", "_chain": {"chain_id": "chat-1"}})
    assert out["ok"] is True and out["value"] == {"thing_id": "t1"}
    nxt = out["chain"]["next"][0]
    assert nxt["skill"] == "t.drop" and nxt["with"] == {"thing": "t1"} and nxt["needs_approval"] is True


def test_a_destructive_call_from_chat_plans_instead_of_running(world):
    _, _, ran = world
    out = chat_tools._execute_skill_tool("t__drop", {"thing": "t1"})
    assert out["ok"] is False and ran == []
    assert out["chain"]["unmet"][0]["id"] == "approved-plan"
    assert out["chain"]["produced"][0]["kind"] == "plan_hash"


def test_the_chain_argument_does_not_read_as_an_undeclared_parameter(world):
    out = chat_tools._execute_skill_tool("t__make", {"q": "a", "_chain": {"chain_id": "c"}, "zzz": 1})
    assert out["ok"] is False and "zzz" in out["errors"][0] and "_chain" not in out["errors"][0]


def test_capability_search_returns_the_chaining_fields(world):
    reg, _, _ = world
    index = build_verb_index(registry=reg, extensions=[])
    hit = next(e for e in search_verbs(index, "make a thing") if e.name == "t.make")
    row = describe(hit, [])
    assert row["risk"] == "reversible-write"
    assert row["requires"] == [{"id": "go", "fix": "pass q"}]
    assert row["produces"] == ["thing_id"] and row["next"] == ["t.drop"] and row["recipes"] == ["r"]


def test_an_undeclared_capability_answers_search_exactly_as_before():
    reg = SkillRegistry()
    reg.register_skill(SkillSpec(name="t.old", fn=lambda p, c: SkillResult(), description="an old thing", surfaces=CHAT))
    row = describe(build_verb_index(registry=reg, extensions=[])[0], [])
    assert not ({"risk", "requires", "produces", "next", "recipes"} & set(row))
