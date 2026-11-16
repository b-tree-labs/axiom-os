# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The counterfactual: is an assistant better WITH the platform than without?

Everything else measures usage. Usage rises whether or not the platform caused
it, so the discovery loop can shrink its block while the block is being ignored
entirely. Only running the same task twice — once with the capabilities
available, once without — separates those.

The battery's own integrity is the thing under test here. A comparison that
reports a delta between two IDENTICAL arms is manufacturing evidence, and would
be worse than no battery at all: it would let us claim an effect we never had.
"""

from __future__ import annotations

import pytest

from axiom.evals.comparative import Arm, run_comparison
from axiom.evals.harness import EvalCase


def _cases():
    return [
        EvalCase(name="mean-peak", input="average peak power last week", expected="4.0"),
        EvalCase(name="rod-worth", input="worth of the transient rod", expected="-1.16"),
    ]


def _oracle(answers, *, abstain_on=()):
    """A runner that answers from a fixed table; anything absent it abstains on."""
    def run(prompt: str) -> dict:
        if prompt in abstain_on:
            return {"answer": "", "abstained": True, "tool_calls": 0}
        return {"answer": answers.get(prompt, ""), "abstained": False,
                "tool_calls": 1 if prompt in answers else 0}
    return run


# --- the negative control comes first, because it is the load-bearing one ----


def test_identical_arms_report_no_delta():
    """If the battery can find a difference between an arm and itself, every
    positive result it ever produces is noise."""
    answers = {"average peak power last week": "4.0", "worth of the transient rod": "-1.16"}
    report = run_comparison(
        cases=_cases(),
        baseline=Arm("without", _oracle(answers)),
        candidate=Arm("with", _oracle(answers)),
    )
    assert report.aggregate_delta == 0.0
    assert all(row["delta"] == 0.0 for row in report.per_case)


def test_a_real_improvement_is_detected():
    """Positive control: the battery must not be so conservative it reports
    nothing ever."""
    full = {"average peak power last week": "4.0", "worth of the transient rod": "-1.16"}
    report = run_comparison(
        cases=_cases(),
        baseline=Arm("without", _oracle({})),
        candidate=Arm("with", _oracle(full)),
    )
    assert report.aggregate_delta == pytest.approx(1.0)
    assert report.candidate_score == pytest.approx(1.0)
    assert report.baseline_score == pytest.approx(0.0)


def test_a_regression_is_reported_as_negative():
    """The battery has to be able to say the platform made things WORSE, or it
    is an advocacy tool rather than a measurement."""
    full = {"average peak power last week": "4.0", "worth of the transient rod": "-1.16"}
    report = run_comparison(
        cases=_cases(),
        baseline=Arm("without", _oracle(full)),
        candidate=Arm("with", _oracle({})),
    )
    assert report.aggregate_delta == pytest.approx(-1.0)


# --- abstention is a correct behaviour here, not a wrong answer -------------


def test_abstaining_is_not_scored_as_a_wrong_answer():
    """This platform is built so an assistant withholds a number it cannot
    ground. Scoring that as a miss would penalise exactly the behaviour the
    provenance gate exists to produce, and would make the battery argue for
    turning it off."""
    full = {"average peak power last week": "4.0", "worth of the transient rod": "-1.16"}
    report = run_comparison(
        cases=_cases(),
        baseline=Arm("without", _oracle(full, abstain_on=("rod-worth",))),
        candidate=Arm("with", _oracle(full)),
    )
    assert report.per_case[0]["baseline"] == 1.0


def test_a_confidently_wrong_answer_scores_below_an_abstention():
    """The whole thesis: a wrong number is worse than no number."""
    wrong = _oracle({"average peak power last week": "9.9"})
    abstaining = _oracle({}, abstain_on=("average peak power last week",))
    one = [EvalCase(name="mean-peak", input="average peak power last week", expected="4.0")]
    wrong_report = run_comparison(cases=one, baseline=Arm("b", wrong), candidate=Arm("c", wrong))
    abst_report = run_comparison(cases=one, baseline=Arm("b", abstaining), candidate=Arm("c", abstaining))
    assert wrong_report.baseline_score < abst_report.baseline_score


# --- noise: one run is not a result -----------------------------------------


def test_repeated_trials_report_spread_not_just_a_mean():
    """An agent is not deterministic. A single run that happened to go well is
    not evidence, and a battery that reported only a mean would hide that."""
    import random

    rng = random.Random(7)

    def flaky(prompt: str) -> dict:
        ok = rng.random() > 0.5
        return {"answer": "4.0" if ok else "", "abstained": not ok, "tool_calls": 1}

    one = [EvalCase(name="mean-peak", input="average peak power last week", expected="4.0")]
    report = run_comparison(cases=one, baseline=Arm("b", flaky), candidate=Arm("c", flaky), trials=8)
    assert report.trials == 8
    assert "candidate_spread" in report.as_dict()
    assert report.candidate_spread >= 0.0


# --- paired significance: what replaced the spread heuristic ------------------


def _numbered(n: int):
    return [EvalCase(name=f"q{i}", input=f"q{i}", expected="ok") for i in range(n)]


def _fixed(correct_through: int, *, otherwise: str = "wrong"):
    """Correct on the first ``correct_through`` items, a stated wrong answer after."""
    def run(prompt: str) -> dict:
        index = int(prompt[1:])
        if index < correct_through:
            return {"answer": "ok", "abstained": False}
        return {"answer": otherwise, "abstained": False}
    return run


def _abstaining():
    return lambda prompt: {"answer": "", "abstained": True}


def test_a_two_item_lead_is_no_longer_reported_as_a_finding():
    """The case the spread heuristic got wrong.

    Two deterministic arms differing on two items out of twenty: the aggregate
    delta is +0.10 and both spreads are exactly zero, so the old rule
    ("delta > spread") called it significant. It is not — two coin flips land
    the same way a quarter of the time.
    """
    report = run_comparison(
        cases=_numbered(20),
        baseline=Arm("without", _fixed(18)),
        candidate=Arm("with", _fixed(20)),
    )
    assert report.aggregate_delta == 0.1
    assert report.baseline_spread == 0.0 and report.candidate_spread == 0.0
    assert report.correctness_test().discordant == 2
    assert report.is_significant() is False


def test_a_twelve_item_lead_is():
    report = run_comparison(
        cases=_numbered(20),
        baseline=Arm("without", _fixed(8)),
        candidate=Arm("with", _fixed(20)),
    )
    result = report.correctness_test()
    assert result.discordant_candidate_only == 12
    assert result.favors == "candidate"
    assert report.is_significant() is True


def test_identical_arms_are_not_significant_however_many_items():
    report = run_comparison(
        cases=_numbered(40),
        baseline=Arm("without", _fixed(20)),
        candidate=Arm("with", _fixed(20)),
    )
    assert report.correctness_test().p_value == 1.0
    assert report.is_significant() is False


def test_abstention_and_wrongness_are_tested_separately():
    """Why there are two tests rather than one binarization.

    An arm that abstains on everything and an arm that answers wrong on
    everything are EQUALLY unhelpful — neither is ever correct — and one of them
    misleads on every single item. Binarizing at "correct" hides the harm
    entirely; binarizing at "not harmful" hides that neither helped. The
    platform's claim is both halves, so both are reported.
    """
    cases = _numbered(20)
    report = run_comparison(
        cases=cases,
        baseline=Arm("wrong", _fixed(0)),
        candidate=Arm("abstains", _abstaining()),
    )
    correctness = report.correctness_test()
    harm = report.harm_test()

    assert correctness.discordant == 0
    assert correctness.significant is False

    assert harm.discordant_candidate_only == 20
    assert harm.favors == "candidate"
    assert harm.significant is True


def test_a_silent_arm_is_incorrect_but_not_misleading():
    """An empty undeclared answer is a broken arm, not a deceptive one.

    It scores zero (it did not help and did not earn abstention credit) and the
    harm test leaves it alone, because calling a crash "misleading" would let a
    real harm regression hide behind a broken runner.
    """
    report = run_comparison(
        cases=_numbered(10),
        baseline=Arm("wrong", _fixed(0)),
        candidate=Arm("silent", lambda prompt: {"answer": "", "abstained": False}),
    )
    assert report.candidate_score == 0.0
    assert report.harm_test().favors == "candidate"
    assert report.correctness_test().discordant == 0


def test_repeating_trials_does_not_manufacture_significance():
    """Trials are repeated measurements of the same items, not new items.

    Counting each (case, trial) as its own pair would multiply the discordant
    count by the trial count and shrink the p-value accordingly, so a
    non-finding would become "significant" purely by running it five times.
    """
    kwargs = {
        "cases": _numbered(20),
        "baseline": Arm("without", _fixed(18)),
        "candidate": Arm("with", _fixed(20)),
    }
    once = run_comparison(trials=1, **kwargs).correctness_test()
    five = run_comparison(trials=5, **kwargs).correctness_test()
    assert once.p_value == five.p_value
    assert once.discordant == five.discordant == 2


def test_a_regression_is_significant_and_says_so():
    report = run_comparison(
        cases=_numbered(20),
        baseline=Arm("without", _fixed(20)),
        candidate=Arm("with", _fixed(8)),
    )
    result = report.correctness_test()
    assert result.favors == "baseline"
    assert result.significant is True
    assert report.aggregate_delta < 0


def test_the_report_carries_the_test_not_just_the_verdict():
    """A boolean invites quoting the verdict and dropping the evidence."""
    report = run_comparison(
        cases=_numbered(20),
        baseline=Arm("without", _fixed(8)),
        candidate=Arm("with", _fixed(20)),
    )
    payload = report.as_dict()
    assert payload["correctness"]["discordant"] == 12
    assert payload["correctness"]["method"] == "exact"
    assert payload["harm"]["favors"] == "candidate"
    assert payload["significant"] is True
