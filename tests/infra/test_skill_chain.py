# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The chaining layer's runtime (ADR-185), through the real dispatch chokepoint.

Every test drives ``invoke_capability`` against a real ``SkillRegistry``, a real
approval queue and the real action audit chain, in a temp state directory. The
claims:

- an undeclared skill behaves exactly as before: no envelope key, no audit field;
- an unmet precondition is a structured refusal carrying the exact fix, and the
  skill body does not run;
- the envelope's ``next`` names only what exists and is exposed on the surface,
  and carries ready-to-use arguments fed from produced refs;
- a destructive skill refuses without an approved plan hash, the hash binds the
  parameters, and an approval is spent by one call;
- a chain id supplied by the caller lands in the audit record, and the chain
  still verifies.
"""

from __future__ import annotations

import logging

import pytest

from axiom.infra import audit_trail
from axiom.infra.orchestrator.approval import ApprovalGate
from axiom.infra.orchestrator.approval_store import FileActionStore
from axiom.infra.skill_chain import plan_digest, split_chain_arg
from axiom.infra.skill_declarations import NextStep, Precondition, Ref
from axiom.infra.skill_dispatch import invoke_capability
from axiom.infra.skills import SkillContext, SkillRegistry, SkillResult, SkillSpec


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("AXI_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("AXIOM_AUTHORITY_RECEIPTS", "off")
    monkeypatch.delenv("AXIOM_DB_URL", raising=False)
    audit_trail._reset_for_tests()
    from axiom.infra import authority

    monkeypatch.setattr(authority, "_default_ctx", authority.build_tool_context(session_factory=None))


class World:
    """A tiny stateful domain for the fixture skills; nothing here names a real one."""

    def __init__(self) -> None:
        self.items: set[str] = set()
        self.dropped: list[str] = []
        self.runs: list[str] = []


def build(world: World) -> SkillRegistry:
    reg = SkillRegistry()
    mcp = ("cli", "mcp", "agent_tool")

    def make(params, ctx):
        world.runs.append("make")
        world.items.add(params["item"])
        return SkillResult(ok=True, value={"item_id": params["item"]})

    def look(params, ctx):
        world.runs.append("look")
        found = sorted(world.items)
        return SkillResult(ok=True, value={"item_id": found[0] if found else "", "count": len(found)})

    def drop(params, ctx):
        world.runs.append("drop")
        world.dropped.append(params["item"])
        world.items.discard(params["item"])
        return SkillResult(ok=True, value={"dropped": params["item"]})

    def plain(params, ctx):
        return SkillResult(ok=True, value={"x": 1}, actions_taken=["did a thing"])

    def dynamic(params, ctx):
        return SkillResult(
            ok=True,
            value={"item_id": "dyn"},
            next=[
                {"skill": "fx.look", "why": "see what exists now"},
                {"skill": "fx.nowhere", "why": "does not exist; must be dropped"},
                {"why": "malformed, no target"},
            ],
        )

    reg.register_skill(SkillSpec(name="fx.plain", fn=plain, description="plain", surfaces=mcp, side_effects=True))
    reg.register_skill(
        SkillSpec(
            name="fx.look",
            fn=look,
            description="look",
            surfaces=mcp,
            side_effects=False,
            risk="read",
            requires=(),
            produces=(Ref("item_id", "pass as item to fx.drop or fx.make"),),
            next_steps=(
                NextStep("fx.drop", "produced:item_id", "remove it when done", {"item": "item_id"}),
                NextStep("fx.nowhere", "ok", "unregistered; must not be offered"),
                NextStep("fx.cli_only", "ok", "registered but not exposed on mcp"),
                NextStep("/where", "ok", "a slash command"),
                NextStep("fx.make", "error", "only on error"),
            ),
        ),
        mutating=False,
    )
    reg.register_skill(
        SkillSpec(
            name="fx.cli_only",
            fn=plain,
            description="cli only",
            surfaces=("cli",),
            side_effects=False,
            risk="read",
        ),
        mutating=False,
    )
    reg.register_skill(
        SkillSpec(
            name="fx.make",
            fn=make,
            description="make an item",
            surfaces=mcp,
            side_effects=True,
            risk="reversible-write",
            inputs={"item": "str!"},
            requires=(
                Precondition(
                    "ledger-open",
                    "the item ledger must be open",
                    check=lambda p, c: ("open" in p) or (False, "the ledger is closed"),
                    fix="pass open=true, or run: axi fx open --item {item}",
                ),
            ),
            produces=(Ref("item_id", "the id of the item that was made"),),
        )
    )
    reg.register_skill(
        SkillSpec(
            name="fx.drop",
            fn=drop,
            description="drop an item",
            surfaces=mcp,
            side_effects=True,
            risk="destructive",
            inputs={"item": "str!"},
            requires=(
                Precondition(
                    "item-id",
                    "an item id from fx.look or fx.make is needed",
                    consumes="item_id",
                    param="item",
                    fix="call fx.look and pass its item_id as item",
                ),
            ),
        )
    )
    reg.register_skill(
        SkillSpec(name="fx.dynamic", fn=dynamic, description="d", surfaces=mcp, side_effects=True, risk="reversible-write",
                  requires=(), produces=(Ref("item_id", "dynamic item"),))
    )
    return reg


@pytest.fixture
def world():
    return World()


@pytest.fixture
def reg(world):
    return build(world)


@pytest.fixture
def ctx(reg, tmp_path):
    return SkillContext(registry=reg, state_dir=tmp_path / "state", logger=logging.getLogger("t"), user_prompt=None)


def call(reg, name, params, ctx, surface="mcp", **kw):
    return invoke_capability(reg, name, params, ctx, surface=surface, **kw)


# ---------------------------------------------------------------- unset == as today


def test_a_skill_that_declares_nothing_carries_no_envelope(reg, ctx):
    r = call(reg, "fx.plain", {}, ctx)
    assert r.ok and r.value == {"x": 1} and r.actions_taken == ["did a thing"]
    assert r.chain_envelope() is None
    assert r.unmet == [] and r.next == [] and r.produced == []


def test_an_unchained_audit_record_has_no_chain_fields(reg, ctx):
    call(reg, "fx.plain", {}, ctx)
    rec = audit_trail.read_chain(audit_trail.audit_path(ctx.state_dir))[-1]
    assert not ({"chain_id", "step_id", "parent_step"} & set(rec))
    assert audit_trail.verify_chain(audit_trail.audit_path(ctx.state_dir))


# ---------------------------------------------------------------- preconditions


def test_an_unmet_precondition_refuses_with_the_exact_fix_and_does_not_run(reg, ctx, world):
    r = call(reg, "fx.make", {"item": "a1"}, ctx)
    assert not r.ok
    assert world.runs == []  # the body never ran
    assert r.unmet == [
        {"id": "ledger-open", "why": "the ledger is closed", "fix": "pass open=true, or run: axi fx open --item a1"}
    ]
    assert "ledger-open" in r.errors[0] and "axi fx open --item a1" in r.errors[0]
    assert r.chain_envelope()["unmet"][0]["id"] == "ledger-open"


def test_a_met_precondition_lets_the_skill_run(reg, ctx, world):
    r = call(reg, "fx.make", {"item": "a1", "open": True}, ctx)
    assert r.ok and world.runs == ["make"]


def test_a_check_that_raises_counts_as_unmet(ctx):
    reg = SkillRegistry()

    def boom(p, c):
        raise RuntimeError("secret detail must not leak")

    reg.register_skill(
        SkillSpec(
            name="fx.x",
            fn=lambda p, c: SkillResult(),
            surfaces=("mcp",),
            risk="reversible-write",
            requires=(Precondition("p", "needs p", check=boom, fix="do p"),),
        )
    )
    r = invoke_capability(reg, "fx.x", {}, ctx, surface="mcp")
    assert not r.ok and r.unmet[0]["id"] == "p" and "secret detail" not in r.unmet[0]["why"]


def test_a_consumed_ref_must_be_supplied(reg, ctx, world):
    # fx.drop on the cli surface (ungated) so only the consumes precondition is in play.
    r = call(reg, "fx.drop", {}, ctx, surface="cli")
    assert not r.ok and r.unmet[0]["id"] == "item-id" and world.runs == []


def test_preconditions_are_audited_as_refusals(reg, ctx):
    call(reg, "fx.make", {"item": "a1"}, ctx, chain={"chain_id": "c-1"})
    rec = audit_trail.read_chain(audit_trail.audit_path(ctx.state_dir))[-1]
    assert rec["skill"] == "fx.make" and rec["outcome"] == "refused" and rec["chain_id"] == "c-1"


# ---------------------------------------------------------------- envelope


def test_next_names_only_what_exists_and_is_exposed(reg, ctx, world):
    world.items.add("i1")
    r = call(reg, "fx.look", {}, ctx)
    env = r.chain_envelope()
    assert env["v"] == 1
    targets = [n.get("skill") or n.get("command") for n in env["next"]]
    assert "fx.drop" in targets
    assert "fx.nowhere" not in targets  # unregistered
    assert "fx.cli_only" not in targets  # not exposed on mcp
    assert "/where" not in targets  # a slash command is unreachable over a protocol
    assert "fx.make" not in targets  # when=error, and the call succeeded
    drop = next(n for n in env["next"] if n.get("skill") == "fx.drop")
    assert drop["with"] == {"item": "i1"}  # fed from the produced ref
    assert drop["risk"] == "destructive" and drop["needs_approval"] is True
    assert drop["tool"] == "axiom_fx__drop"


def test_the_same_result_offers_a_slash_command_in_chat(reg, ctx, world):
    world.items.add("i1")
    r = call(reg, "fx.look", {}, ctx, surface="chat")
    targets = [n.get("skill") or n.get("command") for n in r.chain_envelope()["next"]]
    assert "/where" in targets and "fx.cli_only" not in targets


def test_produced_refs_carry_value_and_use(reg, ctx, world):
    world.items.add("i1")
    r = call(reg, "fx.look", {}, ctx)
    assert r.produced == [{"kind": "item_id", "value": "i1", "how_to_use": "pass as item to fx.drop or fx.make"}]
    assert r.value == {"item_id": "i1", "count": 1}  # existing fields untouched


def test_a_produced_step_is_withheld_when_nothing_was_produced(reg, ctx):
    r = call(reg, "fx.look", {}, ctx)  # no items: item_id == ""
    assert r.produced == []
    assert all(n.get("skill") != "fx.drop" for n in (r.chain_envelope() or {}).get("next", []))


def test_dynamic_next_is_validated_like_declared_next(reg, ctx):
    r = call(reg, "fx.dynamic", {}, ctx)
    targets = [n.get("skill") for n in r.next]
    assert targets == ["fx.look"]


def test_a_failed_call_offers_its_error_steps(reg, ctx):
    reg2 = SkillRegistry()
    reg2.register_skill(SkillSpec(name="fx.a", fn=lambda p, c: SkillResult(ok=False, errors=["no"]),
                                  surfaces=("mcp",), risk="reversible-write",
                                  next_steps=(NextStep("fx.b", "error", "diagnose"), NextStep("fx.c", "ok", "go on"))))
    reg2.register_skill(SkillSpec(name="fx.b", fn=lambda p, c: SkillResult(), surfaces=("mcp",), risk="read", side_effects=False))
    reg2.register_skill(SkillSpec(name="fx.c", fn=lambda p, c: SkillResult(), surfaces=("mcp",), risk="read", side_effects=False))
    r = invoke_capability(reg2, "fx.a", {}, ctx, surface="mcp")
    assert [n["skill"] for n in r.next] == ["fx.b"]


# ---------------------------------------------------------------- plan / apply


def _gate(ctx):
    return ApprovalGate(FileActionStore(ctx.state_dir / "orchestrator" / "approvals.json"))


def test_a_destructive_call_without_a_plan_hash_opens_a_plan_and_does_not_run(reg, ctx, world):
    world.items.add("i1")
    r = call(reg, "fx.drop", {"item": "i1"}, ctx)
    assert not r.ok and world.dropped == []
    assert r.unmet[0]["id"] == "approved-plan"
    h = plan_digest("fx.drop", {"item": "i1"})
    assert r.value["plan_hash"] == h
    assert {"kind": "plan_hash", "value": h} == {k: r.produced[0][k] for k in ("kind", "value")}
    assert f"plan_hash={h}" in r.unmet[0]["fix"] and r.value["held_action_id"] in r.unmet[0]["fix"]
    assert len(_gate(ctx).pending()) == 1


def test_a_forged_hash_without_an_approval_gains_nothing(reg, ctx, world):
    world.items.add("i1")
    h = plan_digest("fx.drop", {"item": "i1"})  # anyone can compute this
    r = call(reg, "fx.drop", {"item": "i1", "plan_hash": h}, ctx)
    assert not r.ok and world.dropped == []
    assert r.unmet[0]["id"] == "approved-plan"  # it opened a plan; it did not apply one


def test_an_approved_plan_applies_once(reg, ctx, world):
    world.items.add("i1")
    first = call(reg, "fx.drop", {"item": "i1"}, ctx)
    _gate(ctx).approve(first.value["held_action_id"], decided_by="@person:test")
    h = first.value["plan_hash"]
    ok = call(reg, "fx.drop", {"item": "i1", "plan_hash": h}, ctx)
    assert ok.ok and world.dropped == ["i1"]
    replay = call(reg, "fx.drop", {"item": "i1", "plan_hash": h}, ctx)
    assert not replay.ok and world.dropped == ["i1"]  # the approval was spent
    assert _gate(ctx).get(first.value["held_action_id"]).status.value == "completed"


def test_the_hash_binds_the_parameters(reg, ctx, world):
    world.items.update({"i1", "i2"})
    first = call(reg, "fx.drop", {"item": "i1"}, ctx)
    _gate(ctx).approve(first.value["held_action_id"], decided_by="@person:test")
    swapped = call(reg, "fx.drop", {"item": "i2", "plan_hash": first.value["plan_hash"]}, ctx)
    assert not swapped.ok and world.dropped == []
    assert swapped.unmet[0]["id"] == "plan-matches-call"


def test_a_rejected_plan_does_not_apply(reg, ctx, world):
    world.items.add("i1")
    first = call(reg, "fx.drop", {"item": "i1"}, ctx)
    _gate(ctx).reject(first.value["held_action_id"], "no", decided_by="@person:test")
    r = call(reg, "fx.drop", {"item": "i1", "plan_hash": first.value["plan_hash"]}, ctx)
    assert not r.ok and world.dropped == []


def test_the_skill_never_sees_the_plan_hash(reg, ctx, world):
    seen = []
    reg2 = SkillRegistry()
    reg2.register_skill(SkillSpec(name="fx.d", fn=lambda p, c: (seen.append(dict(p)), SkillResult())[1],
                                  surfaces=("mcp",), risk="destructive", requires=()))
    first = invoke_capability(reg2, "fx.d", {"a": 1}, ctx, surface="mcp")
    _gate(ctx).approve(first.value["held_action_id"], decided_by="@person:test")
    invoke_capability(reg2, "fx.d", {"a": 1, "plan_hash": first.value["plan_hash"]}, ctx, surface="mcp")
    assert seen == [{"a": 1}]


def test_the_cli_surface_is_not_plan_gated(reg, ctx, world):
    world.items.add("i1")
    r = call(reg, "fx.drop", {"item": "i1"}, ctx, surface="cli")
    assert r.ok and world.dropped == ["i1"]


# ---------------------------------------------------------------- chain context


def test_chain_ids_reach_the_audit_and_the_chain_still_verifies(reg, ctx, world):
    a = call(reg, "fx.make", {"item": "a1", "open": True}, ctx, chain={"chain_id": "run-1"})
    step_a = a.chain_envelope()["step_id"]
    call(reg, "fx.make", {"item": "a2", "open": True}, ctx, chain={"chain_id": "run-1", "parent_step": step_a})
    recs = audit_trail.read_chain(audit_trail.audit_path(ctx.state_dir))
    assert [r["chain_id"] for r in recs] == ["run-1", "run-1"]
    assert recs[0]["step_id"] == step_a and "parent_step" not in recs[0]
    assert recs[1]["parent_step"] == step_a and recs[1]["step_id"] != step_a
    assert audit_trail.verify_chain(audit_trail.audit_path(ctx.state_dir))


def test_a_tampered_chain_field_breaks_the_audit_chain(reg, ctx):
    call(reg, "fx.make", {"item": "a1", "open": True}, ctx, chain={"chain_id": "run-1"})
    path = audit_trail.audit_path(ctx.state_dir)
    path.write_text(path.read_text().replace("run-1", "run-2"))
    assert not audit_trail.verify_chain(path)


def test_split_chain_arg_drops_malformed_bookkeeping():
    args, chain = split_chain_arg({"a": 1, "_chain": {"chain_id": "ok-1", "parent_step": "p1"}})
    assert args == {"a": 1} and chain == {"chain_id": "ok-1", "parent_step": "p1"}
    assert split_chain_arg({"_chain": {"chain_id": "bad id!"}})[1] is None
    assert split_chain_arg({"_chain": "nope"})[1] is None


# ---------------------------------------------------------------- declarations


def test_declarations_validate():
    with pytest.raises(ValueError):
        Precondition("x", "d", fix="")
    with pytest.raises(ValueError):
        NextStep("a.b", "sometimes", "why")
    with pytest.raises(ValueError):
        SkillSpec(name="a.b", fn=lambda p, c: None, risk="dangerous")
    with pytest.raises(ValueError):
        SkillSpec(name="a.b", fn=lambda p, c: None,
                  requires=(Precondition("x", "d", fix="f"), Precondition("x", "d", fix="f")))
    with pytest.raises(TypeError):
        SkillSpec(name="a.b", fn=lambda p, c: None, requires=("not a precondition",))


def test_two_callers_holding_one_approved_plan_cannot_both_apply_it(reg, ctx, world):
    """The approval is spent BEFORE the body runs (compare-and-set), not after.

    Interleave the two calls the way two processes would: both prepare while the
    plan is still approved. The second must be refused even though the first has
    not finished.
    """
    from axiom.infra import skill_chain

    world.items.add("i1")
    first = call(reg, "fx.drop", {"item": "i1"}, ctx)
    _gate(ctx).approve(first.value["held_action_id"], decided_by="@person:test")
    params = {"item": "i1", "plan_hash": first.value["plan_hash"]}
    a = skill_chain.prepare(reg, "fx.drop", dict(params), ctx, surface="mcp")
    b = skill_chain.prepare(reg, "fx.drop", dict(params), ctx, surface="mcp")
    assert a.refusal is None and a.claimed is not None
    assert b.refusal is not None and not b.refusal.ok  # the approval is already spent
    assert world.dropped == []  # (neither body has run; only the claim was taken)


def test_next_never_offers_an_unregistered_skill_on_any_surface(reg, ctx, world):
    world.items.add("i1")
    for surface in ("cli", "mcp", "chat"):
        r = call(reg, "fx.look", {}, ctx, surface=surface)
        offered = {n.get("skill") for n in r.next}
        assert "fx.nowhere" not in offered, surface
        assert all(reg.has(s) for s in offered if s), surface
