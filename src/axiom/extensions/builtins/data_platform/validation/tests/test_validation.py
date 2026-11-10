# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""What the validation toolkit guarantees.

The properties here are the ones that make a QC gate trustworthy rather than
decorative. Most of them are about the difference between a check that failed
and a check that could not run, because collapsing those two is how every
false-green in this portfolio has happened.
"""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.data_platform.validation import (
    Check,
    CheckRegistry,
    Outcome,
    Subject,
    Verdict,
    paired,
    run_checks,
    within_tolerance,
)

REF = "model:example-surrogate@3"


def _verdict(outcome: Outcome, name: str = "c") -> Verdict:
    return Verdict(check=name, model_ref=REF, outcome=outcome)


def _subject(**kw) -> Subject:
    return Subject(model_ref=REF, **kw)


# ---------------------------------------------------------------------------
# The distinction the whole module exists to protect
# ---------------------------------------------------------------------------


def test_a_check_that_raises_is_an_error_not_a_failure():
    """A broken check says nothing about the model.

    If a raised exception became FAIL, a QC score would drop when a query timed
    out, and the report would name the model as the culprit.
    """
    registry = CheckRegistry()
    registry.register(REF, "boom", lambda s: 1 / 0)

    summary = run_checks(_subject(), registry)

    assert summary.errored and not summary.failed
    assert "ZeroDivisionError" in summary.errored[0].detail


def test_a_check_returning_nothing_is_an_error():
    """The other way a check can silently not run."""
    registry = CheckRegistry()
    registry.register(REF, "forgot-return", lambda s: None)

    summary = run_checks(_subject(), registry)

    assert summary.errored[0].outcome is Outcome.ERROR


def test_errors_do_not_enter_the_pass_rate():
    registry = CheckRegistry()
    registry.register(REF, "good", lambda s: _verdict(Outcome.PASS, "good"))
    registry.register(REF, "broken", lambda s: 1 / 0)

    summary = run_checks(_subject(), registry)

    assert summary.pass_rate == 1.0, "one pass, one error: the pass rate is 1/1, not 1/2"
    assert len(summary.errored) == 1


def test_a_run_where_everything_errored_has_no_pass_rate():
    """None, not 0.0 and not 1.0.

    Inventing a number here would report an outage as a result, in whichever
    direction the default happened to point.
    """
    registry = CheckRegistry()
    registry.register(REF, "broken", lambda s: 1 / 0)

    assert run_checks(_subject(), registry).pass_rate is None


def test_one_bad_check_does_not_hide_the_others():
    """A contributor's bug must not cost everyone else their verdicts."""
    registry = CheckRegistry()
    registry.register(REF, "a-broken", lambda s: 1 / 0)
    registry.register(REF, "z-fine", lambda s: _verdict(Outcome.PASS, "z-fine"))

    summary = run_checks(_subject(), registry)

    assert len(summary.verdicts) == 2
    assert summary.passed[0].check == "z-fine"


# ---------------------------------------------------------------------------
# Promotion
# ---------------------------------------------------------------------------


def test_an_errored_check_blocks_promotion():
    """ "We could not check" is not permission to proceed.

    A gate that opens when its checks cannot run is not a gate.
    """
    registry = CheckRegistry()
    registry.register(REF, "good", lambda s: _verdict(Outcome.PASS, "good"))
    registry.register(REF, "broken", lambda s: 1 / 0)

    assert run_checks(_subject(), registry).promotable is False


def test_no_checks_at_all_is_not_promotable():
    """The silent-empty case, which looks exactly like a healthy run.

    An unregistered model_ref means nothing judged it. Promoting on that basis
    would make an empty registry a universal approval.
    """
    summary = run_checks(_subject(), CheckRegistry())

    assert summary.verdicts == ()
    assert summary.promotable is False
    assert "no check reached a judgement" in summary.reasons()


def test_all_passing_is_promotable():
    registry = CheckRegistry()
    registry.register(REF, "a", lambda s: _verdict(Outcome.PASS, "a"))
    registry.register(REF, "b", lambda s: _verdict(Outcome.PASS, "b"))

    assert run_checks(_subject(), registry).promotable is True


def test_skips_are_reported_but_do_not_block():
    """A check that does not apply is not an objection."""
    registry = CheckRegistry()
    registry.register(REF, "pass", lambda s: _verdict(Outcome.PASS, "pass"))
    registry.register(REF, "n/a", lambda s: _verdict(Outcome.SKIP, "n/a"))

    summary = run_checks(_subject(), registry)

    assert summary.promotable is True
    assert len(summary.skipped) == 1
    assert summary.pass_rate == 1.0


def test_ok_is_false_for_an_error():
    """Reading `if v.ok` is safe; reading `if not v.failed` is the trap."""
    assert _verdict(Outcome.ERROR).ok is False
    assert _verdict(Outcome.PASS).ok is True


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------


def test_registering_the_same_name_twice_raises():
    """Installing a package must not be able to silently replace a QC gate."""
    registry = CheckRegistry()
    registry.register(REF, "bounds", lambda s: _verdict(Outcome.PASS))

    with pytest.raises(ValueError, match="already registered"):
        registry.register(REF, "bounds", lambda s: _verdict(Outcome.PASS))


def test_several_checks_may_judge_one_model():
    """Different axes, different owners, different reasons to fail."""
    registry = CheckRegistry()
    registry.register(REF, "vs-measurement", lambda s: _verdict(Outcome.PASS))
    registry.register(REF, "vs-prior-version", lambda s: _verdict(Outcome.PASS))

    assert registry.names(REF) == ["vs-measurement", "vs-prior-version"]


def test_checks_are_scoped_to_a_model_revision():
    """A check registered for @3 must not judge @4.

    Revisions exist because the model changed. A bound that was right for one
    is an assumption about the other.
    """
    registry = CheckRegistry()
    registry.register("corral:rom@3", "bounds", lambda s: _verdict(Outcome.PASS))

    assert run_checks(Subject(model_ref="corral:rom@4"), registry).verdicts == ()


def test_a_check_satisfies_the_protocol():
    def bounds(subject: Subject) -> Verdict:
        return Verdict(check="bounds", model_ref=subject.model_ref, outcome=Outcome.PASS)

    check: Check = bounds
    assert check(_subject()).ok


# ---------------------------------------------------------------------------
# Tolerance
# ---------------------------------------------------------------------------


def test_absolute_tolerance_survives_quantisation():
    """The failure that keeps recurring in this portfolio.

    A signal recorded to the nearest degree, sitting at 15 degrees, moves 6.7%
    when it moves one count. A relative-only gate at 5% calls instrument
    resolution a real change, keeps every frame, and reports itself healthy.
    """
    assert within_tolerance(16.0, 15.0, absolute=1.0, relative=0.05) is True
    assert within_tolerance(16.0, 15.0, absolute=0.0, relative=0.05) is False


def test_relative_tolerance_carries_across_decades():
    """The mirror failure: an absolute-only gate on a wide-range signal."""
    assert within_tolerance(10_500.0, 10_000.0, absolute=1.0, relative=0.10) is True


def test_a_relative_test_against_zero_cannot_pass_on_ratio_alone():
    """Dividing by a zero reference is how a meaningless gate gets written."""
    assert within_tolerance(0.5, 0.0, absolute=0.0, relative=1e9) is False
    assert within_tolerance(0.5, 0.0, absolute=1.0, relative=0.0) is True


def test_nan_is_not_agreement():
    """A hole in the data is not a passing comparison."""
    assert within_tolerance(float("nan"), 1.0, absolute=1e9, relative=1e9) is False
    assert within_tolerance(1.0, float("inf"), absolute=1e9, relative=1e9) is False


# ---------------------------------------------------------------------------
# Pairing
# ---------------------------------------------------------------------------


def test_unmeasured_predictions_are_not_disagreements():
    """A model is not wrong because the instrument was not sampling.

    Counting an unpaired prediction as a miss would penalise a model for the
    measurement schedule, which is the fastest way to make a QC number
    meaningless.
    """
    rows = paired(
        [{"t": 1, "v": 10.0}, {"t": 2, "v": 20.0}],
        [{"t": 1, "v": 10.1}],
        key="t",
        value="v",
    )

    assert rows == [(1, 10.0, 10.1)]


def test_the_registry_name_wins_over_the_check_s_own_name():
    """A verdict must name something you can find in the registry.

    A check knows what it does; the registry knows what it is in this
    deployment. If those could differ, a promotion report would name a failing
    check nobody can look up, and two packages could both call themselves
    "bounds" while registered under different keys.
    """
    registry = CheckRegistry()
    registry.register(
        REF,
        "as-registered",
        lambda s: Verdict(check="what-i-call-myself", model_ref=REF, outcome=Outcome.PASS),
    )

    verdict = run_checks(_subject(), registry).verdicts[0]

    assert verdict.check == "as-registered"
    assert verdict.outcome is Outcome.PASS
