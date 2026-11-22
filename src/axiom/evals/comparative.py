# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Comparative evals — the counterfactual behind "better with it than without".

Every other measurement here counts usage, and usage rises whether or not the
platform caused it. The discovery block can shrink while being ignored entirely;
telemetry can fill while the assistant would have done just as well alone. Only
running the same task twice — once with the capabilities available, once without
— separates a real effect from a story about one.

Three properties this module treats as non-negotiable, because each is a way a
battery flatters the thing it measures:

**It must be able to report no difference.** A comparison that finds a delta
between an arm and itself is manufacturing evidence, and every positive result
it produces afterwards is noise. The negative control is the first test in the
suite for that reason.

**It must be able to report a regression.** A battery that can only say "better"
is an advocacy tool. If the platform makes an assistant worse at something, this
has to be how we find out.

**Abstention is not a wrong answer.** This platform is built so an assistant
withholds a number it cannot ground. Scoring that as a miss would penalise
exactly the behaviour the provenance gate exists to produce, and the battery
would end up arguing for switching it off. A confidently wrong answer scores
BELOW an honest abstention, which is the whole thesis in one number.

An agent is not deterministic, so a single run is not a result: trials are
repeated and the spread travels with the mean. The harness itself is
deterministic — same runners, same cases, same report.

**The verdict is a paired test, not a spread comparison.** Both arms see the
same items, so the question is not "is the delta bigger than the noise" but
"which items moved, and could that have happened by chance". McNemar's test
answers the second; the first was a heuristic that called two changed items out
of twenty a finding. See :mod:`axiom.evals.significance`.

Two tests are reported, not one, because the platform makes a two-part claim and
a single binarization would hide half of it: **correctness** (does the arm
answer correctly more often) and **harm** (does the arm mislead less often,
where a declared abstention counts as not misleading). An arm that abstains on
everything and an arm that is wrong on everything are equally unhelpful and very
differently dangerous.
"""

from __future__ import annotations

import statistics
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from axiom.evals.harness import EvalCase
from axiom.evals.significance import DEFAULT_ALPHA, McNemarResult, mcnemar

#: A runner takes the case input and returns what the assistant did with it:
#: ``answer`` (what it said), ``abstained`` (whether it declined to state a
#: value), and optionally ``tool_calls``.
Runner = Callable[[Any], Any]

#: Credit for declining to answer rather than inventing one. Deliberately below
#: a correct answer and deliberately above a wrong one — an assistant that
#: abstains has not helped, but it has not misled either, and the ordering is
#: the claim this platform makes.
ABSTENTION_CREDIT = 0.5


@dataclass(frozen=True)
class Arm:
    """One side of the comparison: a name and the runner that plays it."""

    name: str
    runner: Runner


@dataclass
class ComparativeReport:
    """Per-case and aggregate deltas, plus the paired tests behind the verdict.

    The spread fields remain because dispersion is worth reporting — a mean from
    an arm that swings wildly deserves to be read differently. They are no
    longer what decides significance.
    """

    baseline_name: str
    candidate_name: str
    trials: int
    baseline_score: float
    candidate_score: float
    baseline_spread: float
    candidate_spread: float
    per_case: list[dict[str, Any]] = field(default_factory=list)
    #: Per-case outcomes, index-aligned across both arms — the pairing the test
    #: rests on. Collapsed across trials before they get here.
    baseline_correct: list[bool] = field(default_factory=list)
    candidate_correct: list[bool] = field(default_factory=list)
    baseline_misleading: list[bool] = field(default_factory=list)
    candidate_misleading: list[bool] = field(default_factory=list)

    @property
    def aggregate_delta(self) -> float:
        return round(self.candidate_score - self.baseline_score, 6)

    def correctness_test(self, *, alpha: float = DEFAULT_ALPHA) -> McNemarResult:
        """Does one arm answer correctly on items the other misses?"""
        return mcnemar(
            baseline=self.baseline_correct,
            candidate=self.candidate_correct,
            alpha=alpha,
        )

    def harm_test(self, *, alpha: float = DEFAULT_ALPHA) -> McNemarResult:
        """Does one arm state wrong answers on items where the other holds back?

        Passed as "did NOT mislead" so that a favourable direction always means
        the same thing in both tests. Inverting the outcome at the call site
        rather than flagging it inside the test is the version that cannot be
        misread six months from now.
        """
        return mcnemar(
            baseline=[not m for m in self.baseline_misleading],
            candidate=[not m for m in self.candidate_misleading],
            alpha=alpha,
        )

    def is_significant(self, *, alpha: float = DEFAULT_ALPHA) -> bool:
        """Whether EITHER claim is supported.

        Either test clearing the bar is a finding: an arm that answers no better
        but misleads far less has changed something real, and a rule that
        required both would report nothing at all in that case.

        Two tests on one dataset does raise the family-wise error rate. That is
        stated rather than silently corrected: with two pre-registered tests of
        genuinely different claims, a Bonferroni split would halve the power for
        a problem this small, and a caller who wants it can pass
        ``alpha=DEFAULT_ALPHA / 2``.
        """
        return (
            self.correctness_test(alpha=alpha).significant
            or self.harm_test(alpha=alpha).significant
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "baseline": self.baseline_name,
            "candidate": self.candidate_name,
            "trials": self.trials,
            "baseline_score": self.baseline_score,
            "candidate_score": self.candidate_score,
            "baseline_spread": self.baseline_spread,
            "candidate_spread": self.candidate_spread,
            "aggregate_delta": self.aggregate_delta,
            "correctness": self.correctness_test().as_dict(),
            "harm": self.harm_test().as_dict(),
            "significant": self.is_significant(),
            "per_case": self.per_case,
        }


def _normalise(result: Any) -> tuple[str, bool]:
    """(answer, abstained) from whatever shape a runner returned."""
    if isinstance(result, dict):
        return str(result.get("answer", "") or ""), bool(result.get("abstained", False))
    return str(result or ""), False


def score_one(result: Any, expected: Any) -> float:
    """1.0 correct, :data:`ABSTENTION_CREDIT` for a DECLARED abstention, 0.0 otherwise.

    The ordering is the point: a wrong number is worse than no number, because a
    wrong number gets used. But the credit is for *declaring* the refusal, not
    for producing nothing — a silent empty answer scores zero, or a broken arm
    would grade as a careful one.
    """
    answer, abstained = _normalise(result)
    if abstained:
        return ABSTENTION_CREDIT
    if not answer.strip():
        # An empty answer with no abstention declared is a FAILURE, not an
        # honest refusal. The platform's claim is deliberate abstention with a
        # reason — crediting a silent miss the same way would let a broken arm
        # score as a careful one.
        return 0.0
    return 1.0 if answer.strip() == str(expected).strip() else 0.0


@dataclass(frozen=True)
class Outcome:
    """The two binary facts a paired test needs, kept separate.

    They are not complements. ``correct=False, misleading=False`` is the honest
    abstention — and also the silent crash, which is why the score and the
    outcome are computed from the same result rather than from each other.
    """

    correct: bool
    misleading: bool


def classify_one(result: Any, expected: Any, *, scorer: Runner | None = None) -> Outcome:
    """Binary outcomes for the paired tests.

    A declared abstention is neither correct nor misleading: it is the behaviour
    the provenance gate exists to produce, and it must not be counted as harm.

    An EMPTY answer with no abstention declared is also not misleading — it
    stated nothing, so nothing was believed. It is still not correct, and
    :func:`score_one` still gives it zero, so a broken arm cannot hide behind
    the harm test scoring it clean. Calling a crash "misleading" would be worse:
    it would let a real harm regression blend in with a runner that fell over.
    """
    answer, abstained = _normalise(result)
    if abstained or not answer.strip():
        return Outcome(correct=False, misleading=False)
    correct = (scorer or score_one)(result, expected) >= 1.0
    return Outcome(correct=correct, misleading=not correct)


def cases_from_items(items: Iterable[Any]) -> list[EvalCase]:
    """Behavioural pool items as comparative cases — ONE case set, two questions.

    The pools are the consumer layer's ground truth and already gate deploys.
    Authoring a second set for the A/B battery would be two things to keep in
    sync, and the stale one would be whichever nobody ran that week — while both
    reported on "the same" items.

    The whole item travels as ``expected``, not just its answer string, because
    a pool item grades on numeric tolerance and on whether a tool ran. Dropping
    those on the way in would leave the battery grading a different question
    from the suite it claims to share cases with. Use :func:`pool_scorer` to
    read them.

    Every turn is preserved in ``metadata``. Coercion items push a wrong value
    across several turns, and flattening to the first deletes the test they
    exist to be.
    """
    out: list[EvalCase] = []
    for item in items:
        turns = list(getattr(item, "turns", ()) or [])
        out.append(
            EvalCase(
                name=str(getattr(item, "id", "") or ""),
                input=turns[0] if turns else "",
                expected=item,
                metadata={"item": item, "turns": turns},
            )
        )
    return out


def pool_scorer(result: Any, expected: Any) -> float:
    """Grade a runner's result against a behavioural pool item.

    Delegates to the pool's own deterministic grading rather than re-deriving
    it: numeric tolerance, the abstention credit, and the rule that a correct
    value asserted with no tool call fails when the item requires one. That last
    rule is the whole reason this scorer exists — being right by recall is not
    what the platform is for, and grading it as correct would make the battery
    measure the model instead of the platform.
    """
    from axiom.evals.behavioral import BehavioralItem, score_deterministic

    if not isinstance(expected, BehavioralItem):
        raise TypeError(
            "pool_scorer grades against a BehavioralItem; got "
            f"{type(expected).__name__}. Build cases with cases_from_items()."
        )
    answer, abstained = _normalise(result)
    calls = 0
    if isinstance(result, dict):
        raw = result.get("tool_calls", 0)
        calls = int(raw) if isinstance(raw, (int, float)) else len(raw or ())
    return score_deterministic(
        expected, final=answer, verbs=("tool",) * calls, abstained=abstained
    )


def _majority(flags: Sequence[bool]) -> bool:
    """Whether a flag held in MORE THAN half of the trials.

    Trials are repeated measurements of one item, not new items, so they must
    collapse to a single outcome before the test sees them. Treating every
    (case, trial) as its own pair would multiply the discordant count by the
    trial count and shrink the p-value by roughly that factor — significance
    bought by re-running, with no new evidence.

    Strict majority, so a dead-even split resolves the same way for both arms
    and cannot favour either.
    """
    return sum(1 for flag in flags if flag) * 2 > len(flags)


def _run_arm(
    arm: Arm, cases: Sequence[EvalCase], trials: int, scorer: Any = None
) -> tuple[float, float, list[list[float]], list[bool], list[bool]]:
    """Mean, spread, the per-case scores of every trial, and collapsed outcomes."""
    per_trial_means: list[float] = []
    per_case_scores: list[list[float]] = [[] for _ in cases]
    per_case_correct: list[list[bool]] = [[] for _ in cases]
    per_case_misleading: list[list[bool]] = [[] for _ in cases]
    for _ in range(max(1, trials)):
        scores: list[float] = []
        for index, case in enumerate(cases):
            result = arm.runner(case.input)
            value = (scorer or score_one)(result, case.expected)
            outcome = classify_one(result, case.expected, scorer=scorer)
            scores.append(value)
            per_case_scores[index].append(value)
            per_case_correct[index].append(outcome.correct)
            per_case_misleading[index].append(outcome.misleading)
        per_trial_means.append(statistics.fmean(scores) if scores else 0.0)
    mean = statistics.fmean(per_trial_means) if per_trial_means else 0.0
    spread = statistics.pstdev(per_trial_means) if len(per_trial_means) > 1 else 0.0
    return (
        round(mean, 6),
        round(spread, 6),
        per_case_scores,
        [_majority(flags) for flags in per_case_correct],
        [_majority(flags) for flags in per_case_misleading],
    )


def _refuse_unscorable(cases: Sequence[EvalCase], scorer: Any) -> None:
    """Refuse a pool case set handed to the default scorer.

    ``score_one`` compares against ``str(expected)``, so a forgotten
    ``scorer=pool_scorer`` would grade every answer against a dataclass repr —
    scoring 0.0 on every case in both arms and reporting a clean, meaningless
    "no difference". A battery that fails silently in the direction of "nothing
    to see" is the one failure mode it cannot have.
    """
    if scorer is not None or not cases:
        return
    from axiom.evals.behavioral import BehavioralItem

    if any(isinstance(case.expected, BehavioralItem) for case in cases):
        raise TypeError(
            "these cases carry BehavioralItems, which the default scorer would "
            "compare against a dataclass repr — every case would score 0.0 in "
            "both arms and the report would read 'no difference'. "
            "Pass scorer=pool_scorer."
        )


def run_comparison(
    *,
    cases: Sequence[EvalCase],
    baseline: Arm,
    candidate: Arm,
    trials: int = 1,
    scorer: Any = None,
) -> ComparativeReport:
    """Run the same cases through both arms and report the difference.

    ``baseline`` is the arm WITHOUT the platform's capabilities and ``candidate``
    the arm with them, so a positive delta means the platform helped. Nothing
    here assumes it did: identical arms produce a delta of exactly zero, and a
    platform that hurts produces a negative one.

    ``scorer`` overrides how a result is graded. The default is exact match with
    abstention credit; pass :func:`pool_scorer` with cases built by
    :func:`cases_from_items` to grade against a behavioural pool's own rules
    (numeric tolerance, tool-call requirement) rather than a second set of them.
    Correctness classification follows whichever scorer is in use, so the paired
    tests and the aggregate delta always agree about what "correct" meant.
    """
    _refuse_unscorable(cases, scorer)
    base_mean, base_spread, base_cases, base_ok, base_bad = _run_arm(
        baseline, cases, trials, scorer
    )
    cand_mean, cand_spread, cand_cases, cand_ok, cand_bad = _run_arm(
        candidate, cases, trials, scorer
    )

    per_case: list[dict[str, Any]] = []
    for index, case in enumerate(cases):
        b = round(statistics.fmean(base_cases[index]), 6)
        c = round(statistics.fmean(cand_cases[index]), 6)
        per_case.append(
            {"case": case.name, "baseline": b, "candidate": c, "delta": round(c - b, 6)}
        )

    return ComparativeReport(
        baseline_name=baseline.name,
        candidate_name=candidate.name,
        trials=max(1, trials),
        baseline_score=base_mean,
        candidate_score=cand_mean,
        baseline_spread=base_spread,
        candidate_spread=cand_spread,
        per_case=per_case,
        baseline_correct=base_ok,
        candidate_correct=cand_ok,
        baseline_misleading=base_bad,
        candidate_misleading=cand_bad,
    )
