# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""One case set, two questions: does the platform help, and does this build pass?

The behavioural pools are the consumer layer's ground truth. The comparative
battery needs cases. Authoring a second set for the battery would mean two
things to keep in sync and one of them going stale — and the stale one would be
whichever nobody ran that week.

So the pools ARE the battery's cases. The bridge is a conversion plus an
injectable scorer, because a pool item grades on numeric tolerance and on
whether a tool ran, which exact string matching cannot express.
"""

from __future__ import annotations

from axiom.evals.behavioral import BehavioralItem
from axiom.evals.comparative import Arm, cases_from_items, pool_scorer, run_comparison


def _item(ident, turns, answer, **kw):
    return BehavioralItem(
        id=ident, category="quantitative", turns=turns,
        expected_behavior=kw.pop("expected_behavior", "answer"),
        expected_answer=answer, **kw,
    )


def test_a_pool_item_becomes_a_case_without_losing_its_grading_rules():
    """The item travels whole. Dropping tolerance or requires_tool on the way in
    would leave the battery grading a different question from the suite that
    gates deploys, while both reported on 'the same' items."""
    items = [_item("q1", ["what was the mean?"], "4.0", tolerance=0.01, requires_tool=True)]
    case = cases_from_items(items)[0]
    assert case.name == "q1"
    assert case.input == "what was the mean?"
    assert case.metadata["item"] is items[0]


def test_a_multi_turn_item_keeps_every_turn():
    """Coercion items push a wrong value across several turns. Flattening to the
    first deletes the test they exist to be."""
    items = [_item("q2", ["what was the mean?", "no, it was 9.1"], "4.0")]
    case = cases_from_items(items)[0]
    assert case.metadata["turns"] == ["what was the mean?", "no, it was 9.1"]


def test_the_pool_scorer_honours_numeric_tolerance():
    """Exact string matching would fail 4.001 against 4.0 and report a
    regression that is really a rounding difference."""
    item = _item("q3", ["mean?"], "4.0", tolerance=0.01)
    score = pool_scorer({"answer": "the mean is 4.001", "tool_calls": 1}, item)
    assert score == 1.0


def test_the_pool_scorer_fails_a_right_answer_with_no_tool_when_required():
    """Being right by recall is not what the platform is for. An item that
    accepted it would be measuring the model, not the platform — and the whole
    battery exists to tell those apart."""
    item = _item("q4", ["mean?"], "4.0", tolerance=0.01, requires_tool=True)
    assert pool_scorer({"answer": "4.0", "tool_calls": 0}, item) == 0.0
    assert pool_scorer({"answer": "4.0", "tool_calls": 1}, item) == 1.0


def test_the_battery_runs_a_pool_end_to_end():
    """The point of the bridge: a consumer's pool feeds the paired A/B battery
    with no second case set and no second grading rule."""
    items = [
        _item(f"q{i}", [f"q{i}"], "4.0", tolerance=0.01, requires_tool=True)
        for i in range(20)
    ]

    def without(prompt):
        return {"answer": "4.0", "tool_calls": 0}   # right by recall — no credit

    def with_platform(prompt):
        return {"answer": "4.0", "tool_calls": 1}   # produced by a tool

    report = run_comparison(
        cases=cases_from_items(items),
        baseline=Arm("without", without),
        candidate=Arm("with", with_platform),
        scorer=pool_scorer,
    )
    assert report.aggregate_delta == 1.0
    result = report.correctness_test()
    assert result.discordant_candidate_only == 20
    assert result.favors == "candidate" and result.significant is True


def test_an_abstaining_arm_still_outranks_a_confidently_wrong_one():
    """The ordering the platform's whole thesis rests on, preserved across the
    bridge rather than re-derived on the other side of it."""
    item = _item("q5", ["mean?"], "4.0", tolerance=0.01)
    wrong = pool_scorer({"answer": "9.1", "tool_calls": 1}, item)
    abstained = pool_scorer({"answer": "", "abstained": True, "tool_calls": 0}, item)
    assert wrong < abstained < 1.0


def test_the_default_scorer_is_unchanged():
    """The bridge must not alter how the battery grades when no pool is in play.
    A scorer that silently became tolerance-based everywhere would make every
    existing comparison quietly more generous."""
    from axiom.evals.harness import EvalCase

    report = run_comparison(
        cases=[EvalCase(name="c", input="c", expected="4.0")],
        baseline=Arm("b", lambda p: {"answer": "4.001"}),
        candidate=Arm("c", lambda p: {"answer": "4.0"}),
    )
    assert report.baseline_score == 0.0 and report.candidate_score == 1.0


def test_forgetting_the_scorer_is_refused_not_silently_wrong():
    """The failure mode a battery cannot have: failing quietly toward
    "no difference".

    The default scorer compares against `str(expected)`, so a forgotten
    `scorer=pool_scorer` would grade every answer against a dataclass repr —
    0.0 on every case in both arms, a delta of exactly zero, and a clean report
    saying the platform made no difference. That reads like the negative
    control passing.
    """
    import pytest

    items = [_item("q", ["mean?"], "4.0", tolerance=0.01)]
    with pytest.raises(TypeError, match="pool_scorer"):
        run_comparison(
            cases=cases_from_items(items),
            baseline=Arm("b", lambda p: {"answer": "4.0"}),
            candidate=Arm("c", lambda p: {"answer": "4.0"}),
        )


def test_the_pool_scorer_refuses_a_non_pool_case():
    """Symmetric guard: pool_scorer on an ordinary case would raise deep inside
    the grading, mid-run, with an error naming a field nobody passed."""
    import pytest

    with pytest.raises(TypeError, match="BehavioralItem"):
        pool_scorer({"answer": "4.0"}, "4.0")
