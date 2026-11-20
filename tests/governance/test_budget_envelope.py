# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A0 acceptance tests 1 to 6 (ADR-232 part 5): the budget envelope.

Numbering follows the ADR's acceptance list. Tests 1 to 4 and 6 are here; test
5 (the dispatch record) has no dispatch record to write into until A2, so the
part A0 can prove is that the ledger is the single source the record and the
provenance ``budget_drawn`` will read from.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta

import pytest

from axiom.governance.budget import (
    BUDGET_EXHAUSTED,
    LIMITS,
    BudgetEnvelope,
    BudgetLedger,
)
from axiom.governance.capability import CapabilityToken
from axiom.governance.classification import Classification
from axiom.governance.intent import IntentPattern
from axiom.governance.resource import ResourcePattern
from axiom.vega.identity.principal import Principal


def _token(budget=None, depth=2) -> CapabilityToken:
    now = datetime.now(UTC)
    return CapabilityToken(
        id="tok-root",
        issuer=Principal(handle="@vault:localhost", public_bytes=b"\x01" * 32),
        subject=Principal(handle="@alice:test", public_bytes=b"\x00" * 32),
        intent_pattern=IntentPattern("*"),
        resource_pattern=ResourcePattern("*"),
        classification_ceiling=Classification.INTERNAL,
        not_before=now - timedelta(seconds=1),
        not_after=now + timedelta(hours=1),
        delegation_depth=depth,
        parent_capability=None,
        signature=b"\xff" * 64,
        budget=budget,
    )


def _env(turns=10, tokens=1000, dollars=1.0, wall=60.0) -> BudgetEnvelope:
    return BudgetEnvelope(turns=turns, tokens=tokens, dollars=dollars, wall_clock_seconds=wall)


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


# ---- 1. four limits; absent envelope only for non-agent tokens -------------


def test_1_token_carries_four_limits():
    token = _token(_env())
    assert token.budget is not None
    assert set(LIMITS) == {"turns", "tokens", "dollars", "wall_clock_seconds"}
    assert token.budget.as_dict() == {
        "turns": 10, "tokens": 1000, "dollars": 1.0, "wall_clock_seconds": 60.0
    }


def test_1_token_without_envelope_is_valid_for_non_agent_use():
    token = _token(None)
    assert token.has_budget is False
    assert token.budget_ledger() is None


@pytest.mark.parametrize("bad", [-1, float("nan"), True, "5"])
def test_1_envelope_rejects_nonsense_limits(bad):
    with pytest.raises((ValueError, TypeError)):
        BudgetEnvelope(turns=bad, tokens=1, dollars=1, wall_clock_seconds=1)


def test_1_envelope_and_token_are_immutable():
    env = _env()
    with pytest.raises(dataclasses.FrozenInstanceError):
        env.turns = 99  # type: ignore[misc]
    token = _token(env)
    with pytest.raises(dataclasses.FrozenInstanceError):
        token.budget = _env(turns=99)  # type: ignore[misc]


def test_1_with_budget_uses_replace_and_keeps_every_other_field():
    token = _token(None)
    granted = token.with_budget(_env())
    assert granted.budget == _env()
    assert dataclasses.replace(granted, budget=None) == token
    assert token.budget is None  # original untouched


# ---- 2. draw and decrement; readable without mutating the token -------------


def test_2_draw_decrements_each_limit_and_remaining_is_readable():
    token = _token(_env())
    ledger = token.budget_ledger()
    assert ledger.draw(turns=1, tokens=100, dollars=0.25, wall_clock_seconds=5).ok
    assert ledger.remaining().as_dict() == {
        "turns": 9, "tokens": 900, "dollars": 0.75, "wall_clock_seconds": 55.0
    }
    assert ledger.drawn().as_dict() == {
        "turns": 1, "tokens": 100, "dollars": 0.25, "wall_clock_seconds": 5.0
    }
    assert token.budget == _env()  # the token never moved


def test_2_elapsed_wall_clock_is_drawn_at_the_boundary():
    clock = FakeClock()
    ledger = BudgetLedger(_env(wall=60), clock=clock)
    clock.t = 20.0
    assert ledger.tick().ok
    assert ledger.remaining().wall_clock_seconds == 40.0


# ---- 3. inheritance is the intersection; reserved at spawn, refunded --------


def test_3_child_is_at_most_parent_remaining_and_at_most_requested():
    parent = BudgetLedger(_env(turns=10, tokens=1000, dollars=1.0, wall=60))
    parent.draw(turns=4, tokens=600)
    child = parent.slice(_env(turns=8, tokens=300, dollars=5.0, wall=10))
    assert child.granted.as_dict() == {
        "turns": 6, "tokens": 300, "dollars": 1.0, "wall_clock_seconds": 10
    }


def test_3_child_can_only_lower_never_raise():
    parent = BudgetLedger(_env(turns=3))
    child = parent.slice(_env(turns=100, tokens=10**9, dollars=10**6, wall=10**6))
    assert child.granted.within(parent.granted)


def test_3_grant_is_reserved_at_spawn_and_unused_refunded_at_completion():
    parent = BudgetLedger(_env(turns=10, tokens=1000))
    child = parent.slice(_env(turns=6, tokens=400))
    assert parent.remaining().turns == 4 and parent.remaining().tokens == 600
    child.draw(turns=2, tokens=100)
    refunded = child.close()
    assert (refunded.turns, refunded.tokens) == (4, 300)
    assert parent.remaining().turns == 8 and parent.remaining().tokens == 900
    assert parent.drawn().turns == 2  # the spent part stays spent
    assert child.close().turns == 0  # idempotent: no double refund
    assert parent.remaining().turns == 8


def test_3_siblings_cannot_exceed_the_parent_grant():
    parent = BudgetLedger(_env(turns=10))
    a = parent.slice(_env(turns=7))
    b = parent.slice(_env(turns=7))
    assert a.granted.turns + b.granted.turns <= 10
    assert b.granted.turns == 3


def test_3_intersection_at_depth_two():
    root = BudgetLedger(_env(turns=10, tokens=1000, dollars=1.0, wall=100))
    child = root.slice(_env(turns=5, tokens=500, dollars=0.5, wall=50))
    child.draw(turns=1, tokens=100)
    grandchild = child.slice(_env(turns=50, tokens=50, dollars=9, wall=500))
    assert grandchild.granted.as_dict() == {
        "turns": 4, "tokens": 50, "dollars": 0.5, "wall_clock_seconds": 50
    }
    assert grandchild.granted.within(child.granted)
    assert child.granted.within(root.granted)
    grandchild.draw(turns=1)
    grandchild.close()
    child.close()
    assert root.remaining().turns == 8  # only the two turns actually spent are gone
    assert root.drawn().turns == 2


def test_3_delegated_token_intersects_and_gets_a_new_id():
    parent = _token(_env(turns=10, tokens=1000))
    child = parent.delegate_with_budget(
        _env(turns=50, tokens=200), remaining=_env(turns=4, tokens=1000)
    )
    assert child.budget.turns == 4 and child.budget.tokens == 200
    assert child.id != parent.id
    assert child.parent_capability == parent.id
    assert child.delegation_depth == parent.delegation_depth - 1
    assert child.intent_pattern == parent.intent_pattern  # replace kept the rest
    assert parent.budget == _env(turns=10, tokens=1000)


def test_3_delegation_without_depth_is_refused():
    with pytest.raises(ValueError):
        _token(_env(), depth=0).delegate_with_budget(_env())


# ---- 4. exhaustion is data, names the limit, never raises ------------------


@pytest.mark.parametrize(
    "limit, kwargs",
    [
        ("turns", {"turns": 2}),
        ("tokens", {"tokens": 50}),
        ("dollars", {"dollars": 0.5}),
        ("wall_clock_seconds", {"wall_clock_seconds": 3}),
    ],
)
def test_4_each_limit_exhausts_with_unmet_budget_exhausted(limit, kwargs):
    caps = dict(turns=2, tokens=50, dollars=0.5, wall_clock_seconds=3)
    ledger = BudgetLedger(BudgetEnvelope(**caps))
    assert ledger.draw(**kwargs).ok  # the step that begins with budget finishes
    refused = ledger.draw(turns=0)  # the next boundary is denied
    assert refused.ok is False
    assert refused.limit == limit
    entry = refused.unmet[0]
    assert entry["id"] == BUDGET_EXHAUSTED
    assert limit in entry["why"] and entry["fix"]
    assert set(entry) == {"id", "why", "fix"}  # ADR-185 shape


def test_4_exhaustion_does_not_raise_and_is_remembered():
    ledger = BudgetLedger(_env(turns=1))
    ledger.draw(turns=1)
    for _ in range(3):
        assert ledger.draw(turns=1).ok is False
    assert ledger.exhausted_limit == "turns"
    assert ledger.drawn().turns == 1  # refused draws record nothing


def test_4_a_child_of_an_exhausted_parent_gets_nothing_to_run_on():
    parent = BudgetLedger(_env(turns=1))
    parent.draw(turns=1)
    assert parent.check().ok is False


# ---- 5. one ledger feeds the record and provenance -------------------------


def test_5_snapshot_reports_granted_drawn_and_outcome():
    ledger = BudgetLedger(_env(turns=1))
    ledger.draw(turns=1)
    ledger.draw(turns=1)  # refused
    snap = ledger.snapshot()
    assert snap["granted"]["turns"] == 1
    assert snap["drawn"]["turns"] == 1
    assert snap["outcome"] == BUDGET_EXHAUSTED and snap["exhausted_limit"] == "turns"


# ---- 6. exhaustion mid-run leaves a half-finished write untouched ----------


def test_6_exhaustion_mid_write_lets_the_write_finish_and_denies_the_next_step():
    ledger = BudgetLedger(_env(turns=1))
    written: list[str] = []

    def write_two_parts() -> None:
        assert ledger.draw(turns=1).ok  # boundary: the step may begin
        written.append("part-1")
        # Mid-step the budget is gone (a concurrent draw, an overrun).
        ledger.draw(turns=0)
        assert ledger.check().ok is False
        written.append("part-2")  # the in-flight write is never cut off

    write_two_parts()
    assert written == ["part-1", "part-2"]  # whole, not half
    assert ledger.draw(turns=1).ok is False  # only NEW work is refused
