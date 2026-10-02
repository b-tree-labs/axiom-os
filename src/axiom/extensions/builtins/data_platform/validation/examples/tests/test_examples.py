# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Tests for the worked examples — the ones a contributor copies first.

No database, no gateway, no VPN, no node. If a check needs any of those to be
tested, it is doing two jobs and the query half belongs outside it.
"""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.data_platform.validation import (
    CheckRegistry,
    Outcome,
    Subject,
    run_checks,
)
from axiom.extensions.builtins.data_platform.validation.examples.reproduces_baseline import (
    reproduces_baseline,
)
from axiom.extensions.builtins.data_platform.validation.examples.tracks_reference import (
    tracks_reference,
)

REF = "model:example-surrogate@3"


def _rows(pairs):
    return tuple({"ts": t, "reading": v} for t, v in pairs)


def _subject(predicted, measured=(), baseline=None):
    return Subject(
        model_ref=REF,
        predicted=_rows(predicted),
        measured=_rows(measured),
        context={"baseline": _rows(baseline)} if baseline is not None else {},
    )


# ---------------------------------------------------------------------------
# Validation against measurement
# ---------------------------------------------------------------------------


def test_tracking_measurement_passes():
    verdict = tracks_reference(_subject([(1, 300.0), (2, 310.0)], [(1, 302.0), (2, 311.0)]))
    assert verdict.outcome is Outcome.PASS
    assert verdict.observed == pytest.approx(2.0)


def test_a_real_excursion_fails():
    verdict = tracks_reference(_subject([(1, 300.0)], [(1, 340.0)]))
    assert verdict.outcome is Outcome.FAIL
    assert "1 outside bounds" in verdict.detail


def test_no_overlap_is_an_error_not_a_failure():
    """Absence of evidence is not evidence of a bad model.

    The instrument not sampling is not the model being wrong, and a QC number
    that drops during an outage is measuring the outage.
    """
    assert tracks_reference(_subject([(1, 300.0)], [(9, 300.0)])).outcome is Outcome.ERROR


def test_one_instrument_count_is_not_a_defect():
    """The quantisation case, which a relative-only tolerance gets wrong.

    A signal recorded to the nearest degree at 15 units: one count of
    disagreement is 6.7%, which sails past any relative gate looser than that
    and fails any gate tighter. The absolute tolerance is what makes this a
    pass.
    """
    assert tracks_reference(_subject([(1, 15.0)], [(1, 16.0)])).outcome is Outcome.PASS


def test_relative_tolerance_still_binds_at_power():
    """And the mirror: at 600, 5 is inside absolute, so relative must carry it."""
    assert tracks_reference(_subject([(1, 600.0)], [(1, 604.0)])).outcome is Outcome.PASS
    assert tracks_reference(_subject([(1, 600.0)], [(1, 640.0)])).outcome is Outcome.FAIL


# ---------------------------------------------------------------------------
# Regression against a baseline
# ---------------------------------------------------------------------------


def test_reproducing_the_baseline_passes():
    verdict = reproduces_baseline(
        _subject([(1, 300.0), (2, 310.0)], baseline=[(1, 300.0), (2, 310.0)])
    )
    assert verdict.outcome is Outcome.PASS


def test_floating_point_reassociation_is_not_a_behaviour_change():
    """A refactor may reorder arithmetic. That is not a regression."""
    verdict = reproduces_baseline(_subject([(1, 300.000000001)], baseline=[(1, 300.0)]))
    assert verdict.outcome is Outcome.PASS


def test_a_real_drift_fails():
    verdict = reproduces_baseline(_subject([(1, 301.0)], baseline=[(1, 300.0)]))
    assert verdict.outcome is Outcome.FAIL
    assert "1 of 1 states drifted" in verdict.detail


def test_a_missing_baseline_is_an_error_not_a_pass():
    """ "We lost the baseline" must not read the same as "nothing changed"."""
    assert reproduces_baseline(_subject([(1, 300.0)])).outcome is Outcome.ERROR


# ---------------------------------------------------------------------------
# The two together — the point of the design
# ---------------------------------------------------------------------------


def test_the_two_checks_fail_independently():
    """A refactor can reproduce a baseline perfectly and still be wrong.

    Here the candidate matches its baseline exactly, so regression passes, while
    both disagree with the instrument, so validation fails. One number covering
    both would have reported this model as fine.
    """
    registry = CheckRegistry()
    registry.register(REF, "validation", tracks_reference)
    registry.register(REF, "regression", reproduces_baseline)

    summary = run_checks(
        _subject([(1, 300.0)], measured=[(1, 400.0)], baseline=[(1, 300.0)]), registry
    )

    outcomes = {v.check: v.outcome for v in summary.verdicts}
    assert outcomes == {"validation": Outcome.FAIL, "regression": Outcome.PASS}
    assert summary.promotable is False
    assert summary.pass_rate == 0.5


def test_a_missing_baseline_does_not_hide_a_passing_validation():
    """An errored check blocks promotion without corrupting the pass rate."""
    registry = CheckRegistry()
    registry.register(REF, "validation", tracks_reference)
    registry.register(REF, "regression", reproduces_baseline)

    summary = run_checks(_subject([(1, 300.0)], measured=[(1, 301.0)]), registry)

    assert summary.pass_rate == 1.0, "the one judgement made was a pass"
    assert len(summary.errored) == 1
    assert summary.promotable is False, "an unrunnable check is not permission to promote"
