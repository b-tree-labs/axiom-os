# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: F811  (pytest fixtures are imported and then taken as arguments)

"""The AEOS delta, as behaviour (AEOS 0.2 section 4.11.7): rules AEOS140, 141, 142, 150.

Each contract is run twice. Against the real chokepoint it must report nothing.
Against a runtime made non-conforming on purpose (the one guard it exists to test
is switched off) it must report that rule, so the check is shown to be able to
fail rather than assumed to.
"""

from __future__ import annotations

import pytest

from axiom.infra import chain_conformance as cc
from axiom.infra import skill_chain
from axiom.infra.orchestrator.approval import ApprovalGate
from axiom.infra.orchestrator.approval_store import FileActionStore
from tests.infra.test_skill_chain import (  # noqa: F401  (fixtures)
    World,
    build,
    ctx,
    isolated,
    reg,
    world,
)


def _approve(context):
    gate = ApprovalGate(FileActionStore(context.state_dir / "orchestrator" / "approvals.json"))
    return lambda held: gate.approve(held, decided_by="@person:test")


def test_requires_contract_holds(reg, ctx, world):
    v = cc.check_requires_contract(
        reg,
        "fx.make",
        {"item": "a"},
        ctx,
        effects=lambda: len(world.items),
        remedy=lambda p: {**p, "open": True},
    )
    assert v == []


def test_requires_contract_fails_when_an_unmet_requirement_still_runs(reg, ctx, world, monkeypatch):
    monkeypatch.setattr(skill_chain, "unmet_preconditions", lambda *a, **k: [])
    v = cc.check_requires_contract(
        reg,
        "fx.make",
        {"item": "a"},
        ctx,
        effects=lambda: len(world.items),
        remedy=lambda p: {**p, "open": True},
    )
    assert any(x.startswith("AEOS141") and "unmet" in x for x in v)


def test_requires_contract_fails_when_the_fix_is_empty(world, ctx, reg):
    import dataclasses

    spec = reg.spec("fx.make")
    reg._skills.pop("fx.make")  # noqa: SLF001
    broken = dataclasses.replace(spec, requires=(dataclasses.replace(spec.requires[0], fix="x"),))
    object.__setattr__(broken.requires[0], "fix", "")  # a runtime that lets an empty fix through
    reg.register_skill(broken)
    v = cc.check_requires_contract(
        reg,
        "fx.make",
        {"item": "a"},
        ctx,
        effects=lambda: len(world.items),
        remedy=lambda p: {**p, "open": True},
    )
    assert any("has no fix" in x for x in v)


def test_plan_apply_contract_holds(reg, ctx, world):
    world.items.update({"i1", "i2"})
    v = cc.check_plan_apply_contract(
        reg,
        "fx.drop",
        {"item": "i1"},
        {"item": "i2"},
        ctx,
        approve=_approve(ctx),
        effects=lambda: len(world.dropped),
    )
    assert v == []


def test_plan_apply_contract_fails_when_a_destructive_call_needs_no_plan(
    reg, ctx, world, monkeypatch
):
    world.items.update({"i1", "i2"})
    monkeypatch.setattr(skill_chain, "UNGATED_SURFACES", frozenset({"mcp", "cli", "runner"}))
    v = cc.check_plan_apply_contract(
        reg,
        "fx.drop",
        {"item": "i1"},
        {"item": "i2"},
        ctx,
        approve=_approve(ctx),
        effects=lambda: len(world.dropped),
    )
    assert any(x.startswith("AEOS140") for x in v)


def test_next_prefill_holds(reg, ctx, world):
    world.items.add("i1")
    assert cc.check_next_prefill(reg, "fx.look", {}, ctx) == []


def test_next_prefill_fails_when_the_value_is_not_carried(reg, ctx, world, monkeypatch):
    world.items.add("i1")
    real = skill_chain.next_steps_for

    def stripped(*a, **k):
        out = real(*a, **k)
        for e in out:
            e.pop("with", None)
        return out

    monkeypatch.setattr(skill_chain, "next_steps_for", stripped)
    v = cc.check_next_prefill(reg, "fx.look", {}, ctx)
    assert any(x.startswith("AEOS142") for x in v)


def test_audit_contract_holds(reg, ctx):
    assert cc.check_audit_contract(reg, "fx.make", {"item": "a", "open": True}, ctx) == []


def test_audit_contract_fails_when_the_chain_id_is_not_recorded(reg, ctx, monkeypatch):
    from axiom.infra import audit_trail

    real = audit_trail.record_invocation
    monkeypatch.setattr(
        audit_trail, "record_invocation", lambda **kw: real(**{**kw, "chain": None})
    )
    v = cc.check_audit_contract(reg, "fx.make", {"item": "a", "open": True}, ctx)
    assert any(x.startswith("AEOS150") for x in v)


@pytest.mark.parametrize("rule", ["AEOS140", "AEOS141", "AEOS142", "AEOS150"])
def test_every_runtime_rule_is_in_the_catalogue(rule):
    from axiom.infra.chain_levels import RULE_IDS

    assert rule in RULE_IDS
