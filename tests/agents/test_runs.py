# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A0: RunHandle (H1), run manager over the budget envelope, poll tools (H2)."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import pytest

from axiom.agents.runs import (
    IllegalTransition,
    InMemoryRunStore,
    RunManager,
    RunState,
    register_poll_tools,
)
from axiom.governance.budget import BUDGET_EXHAUSTED, BUDGET_REQUIRED, BudgetEnvelope
from axiom.governance.capability import CapabilityToken
from axiom.infra.principal import PrincipalContext
from axiom.infra.skills import SkillContext, SkillRegistry

ALICE = "@alice:test"


def _env(turns=10, tokens=1000, dollars=1.0, wall=60.0):
    return BudgetEnvelope(turns=turns, tokens=tokens, dollars=dollars, wall_clock_seconds=wall)


def _token(budget=None) -> CapabilityToken:
    t = CapabilityToken.unscoped_test_token(
        subject=__import__("axiom.vega.identity.principal", fromlist=["Principal"]).Principal(
            handle=ALICE, public_bytes=b"\x00" * 32
        )
    )
    return t.with_budget(budget)


def _ctx(registry: SkillRegistry, who: str = ALICE) -> SkillContext:
    return SkillContext(
        registry=registry,
        state_dir=Path("."),
        logger=logging.getLogger("test"),
        principal=PrincipalContext(handle=who),
    )


# ---- H1: refuses to start without an envelope ------------------------------


def test_agent_backed_run_refuses_to_start_without_an_envelope():
    mgr = RunManager()
    out = mgr.submit("x.run", token=_token(None), principal=ALICE)
    assert out.ok is False
    assert out.unmet[0]["id"] == BUDGET_REQUIRED
    assert mgr.list() == []  # nothing was created


def test_non_agent_run_needs_no_envelope():
    mgr = RunManager()
    out = mgr.submit("x.run", token=_token(None), principal=ALICE, agent_backed=False)
    assert out.ok and out.handle.budget is None
    assert mgr.draw(out.handle.id, turns=5).ok  # unbudgeted: nothing to draw from


def test_run_ids_are_generated_and_distinct():
    mgr = RunManager()
    a = mgr.submit("x.run", token=_token(_env()), principal=ALICE).handle
    b = mgr.submit("x.run", token=_token(_env()), principal=ALICE).handle
    assert a.id != b.id and a.id.startswith("run_")


# ---- H1: state machine ------------------------------------------------------


def test_state_machine_happy_path_and_timestamps():
    mgr = RunManager()
    h = mgr.submit("x.run", token=_token(_env()), principal=ALICE).handle
    assert h.state is RunState.SUBMITTED and h.started_at is None
    h = mgr.start(h.id)
    assert h.state is RunState.WORKING and h.started_at is not None
    h = mgr.advance(h.id, RunState.INPUT_NEEDED)
    h = mgr.advance(h.id, RunState.WORKING)
    h = mgr.advance(h.id, RunState.COMPLETED, result_ref="frag:123")
    assert h.terminal and h.finished_at and h.result_ref == "frag:123"
    assert [e.kind for e in mgr.events(h.id)][0] == "submitted"


def test_terminal_states_have_no_exits_and_illegal_moves_raise():
    mgr = RunManager()
    h = mgr.submit("x.run", token=_token(_env()), principal=ALICE).handle
    with pytest.raises(IllegalTransition):
        mgr.advance(h.id, RunState.COMPLETED)  # submitted -> completed is not a move
    mgr.start(h.id)
    mgr.advance(h.id, RunState.COMPLETED)
    for state in (RunState.WORKING, RunState.FAILED, RunState.CANCELLED):
        with pytest.raises(IllegalTransition):
            mgr.advance(h.id, state)


def test_store_interface_is_swappable():
    store = InMemoryRunStore()
    mgr = RunManager(store)
    h = mgr.submit("x.run", token=_token(_env()), principal=ALICE).handle
    assert store.get(h.id) == h
    assert [r.id for r in store.list(state=RunState.SUBMITTED)] == [h.id]


# ---- budget through the run manager ----------------------------------------


def test_run_budget_is_the_token_grant_and_draws_show_on_the_handle():
    mgr = RunManager()
    h = mgr.submit("x.run", token=_token(_env(turns=5)), principal=ALICE).handle
    assert h.budget["granted"]["turns"] == 5
    mgr.start(h.id)
    assert mgr.draw(h.id, turns=2, tokens=10).ok
    assert mgr.get(h.id).budget["drawn"]["turns"] == 2


def test_exhaustion_fails_the_run_with_cause_and_unmet():
    mgr = RunManager()
    h = mgr.submit("x.run", token=_token(_env(turns=1)), principal=ALICE).handle
    mgr.start(h.id)
    assert mgr.draw(h.id, turns=1).ok
    out = mgr.draw(h.id, turns=1)
    assert out.ok is False and out.unmet[0]["id"] == BUDGET_EXHAUSTED
    done = mgr.get(h.id)
    assert done.state is RunState.FAILED and done.cause == BUDGET_EXHAUSTED
    assert done.budget["outcome"] == BUDGET_EXHAUSTED


def test_child_run_draws_a_slice_and_refunds_on_completion():
    mgr = RunManager()
    root = mgr.submit("x.run", token=_token(_env(turns=10)), principal=ALICE).handle
    mgr.start(root.id)
    child = mgr.submit(
        "x.run", token=_token(None), principal=ALICE, parent_id=root.id,
        requested_budget=_env(turns=6),
    ).handle
    assert child.parent_id == root.id and child.budget["granted"]["turns"] == 6
    assert mgr.remaining(root.id).turns == 4
    mgr.start(child.id)
    mgr.draw(child.id, turns=2)
    mgr.advance(child.id, RunState.COMPLETED)
    assert mgr.remaining(root.id).turns == 8


def test_child_of_a_finished_or_missing_parent_is_refused():
    mgr = RunManager()
    assert mgr.submit("x.run", token=_token(_env()), principal=ALICE, parent_id="run_nope").ok is False
    root = mgr.submit("x.run", token=_token(_env()), principal=ALICE).handle
    mgr.cancel(root.id)
    assert mgr.submit("x.run", token=_token(None), principal=ALICE, parent_id=root.id).ok is False


def test_cancel_refunds_the_parent():
    mgr = RunManager()
    root = mgr.submit("x.run", token=_token(_env(turns=10)), principal=ALICE).handle
    child = mgr.submit("x.run", token=_token(None), principal=ALICE, parent_id=root.id,
                       requested_budget=_env(turns=7)).handle
    assert mgr.remaining(root.id).turns == 3
    assert mgr.cancel(child.id).handle.state is RunState.CANCELLED
    assert mgr.remaining(root.id).turns == 10


# ---- H2: poll tools through the registry, on every projection --------------


def _registered():
    mgr = RunManager()
    reg = SkillRegistry()
    register_poll_tools(reg, "demo.longjob", mgr)
    return mgr, reg


def test_poll_tools_register_and_declare_the_three_surfaces():
    _, reg = _registered()
    assert reg.has("demo.longjob.status") and reg.has("demo.longjob.cancel")
    for name in ("demo.longjob.status", "demo.longjob.cancel"):
        spec = reg.spec(name)
        assert set(spec.surfaces) == {"cli", "mcp", "agent_tool"}
        assert spec.inputs == {"run_id": "str!"}
    assert reg.spec("demo.longjob.status").effective_risk == "read"


def test_status_returns_state_budget_and_result_ref():
    mgr, reg = _registered()
    h = mgr.submit("demo.longjob", token=_token(_env(turns=3)), principal=ALICE).handle
    mgr.start(h.id)
    res = reg.invoke("demo.longjob.status", {"run_id": h.id}, _ctx(reg))
    assert res.ok and res.value["state"] == "working"
    assert res.value["budget"]["granted"]["turns"] == 3


def test_cancel_stops_a_run_and_a_second_cancel_reports_it_finished():
    mgr, reg = _registered()
    h = mgr.submit("demo.longjob", token=_token(_env()), principal=ALICE).handle
    res = reg.invoke("demo.longjob.cancel", {"run_id": h.id}, _ctx(reg))
    assert res.ok and res.value["state"] == "cancelled"
    again = reg.invoke("demo.longjob.cancel", {"run_id": h.id}, _ctx(reg))
    assert again.ok is False and again.unmet[0]["id"] == "run_finished"


def test_unknown_and_foreign_runs_look_identical():
    mgr, reg = _registered()
    h = mgr.submit("demo.longjob", token=_token(_env()), principal=ALICE).handle
    foreign = reg.invoke("demo.longjob.status", {"run_id": h.id}, _ctx(reg, "@mallory:test"))
    missing = reg.invoke("demo.longjob.status", {"run_id": "run_nope"}, _ctx(reg))
    assert foreign.ok is False and missing.ok is False
    assert foreign.unmet[0]["id"] == missing.unmet[0]["id"] == "run_not_found"
    assert mgr.get(h.id).state is RunState.SUBMITTED  # and cancel was not possible either
    reg.invoke("demo.longjob.cancel", {"run_id": h.id}, _ctx(reg, "@mallory:test"))
    assert mgr.get(h.id).state is RunState.SUBMITTED


def test_poll_tool_works_from_an_mcp_client():
    from axiom.extensions.builtins.mcp.skill_tools import skill_tool_contribution

    mgr, reg = _registered()
    h = mgr.submit("demo.longjob", token=_token(_env()), principal=ALICE).handle
    contrib = skill_tool_contribution(reg, ctx_factory=lambda: _ctx(reg))
    names = {t.name for t in contrib.tools}
    assert {"axiom_demo__longjob__status", "axiom_demo__longjob__cancel"} <= names
    status_tool = "axiom_demo__longjob__status"
    payload = asyncio.run(contrib.dispatch[status_tool]({"run_id": h.id}))
    assert payload["ok"] is True and payload["value"]["run_id"] == h.id
    cancel_tool = "axiom_demo__longjob__cancel"
    payload = asyncio.run(contrib.dispatch[cancel_tool]({"run_id": h.id}))
    assert payload["value"]["state"] == "cancelled"
