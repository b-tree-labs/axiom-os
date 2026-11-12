# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The consumer's real failure shapes, as tests.

Every case below is a defect this portfolio has actually paid for, written
against the uncertainty primitive to show what it would have said. None of
them is hypothetical and none names the consumer.

The point is not that uncertainty is nice to have. It is that each of these
reached a served value, and in every one the information needed to catch it
either existed upstream or was cheap to declare.
"""

from __future__ import annotations

import math

import pytest

from axiom.uncertainty import (
    Budget,
    MagnitudeOnly,
    Quantity,
    Registry,
    Unquantified,
    add,
    correlation,
    difference,
    mean,
)

# One instrument's calibration is ONE source, however many readings it makes.
BATH = "signals:cal-bath-a:offset"
TC14_REP = "signals:tc-14:repeatability"
TC15_REP = "signals:tc-15:repeatability"
STEP_HOLD = "data_platform:resample:step_hold"
ROM_ERR = "model_corral:rom-v3:surrogate_error"
ROM_EXTRAP = "model_corral:rom-v3:extrapolation"


def _reading(value: float, rep_symbol: str, *, rep: float = 0.1, cal: float = 0.5) -> Quantity:
    return Quantity(value=value, unit="degC", terms={BATH: cal, rep_symbol: rep})


# --- 1. The averaging error a scalar column invites --------------------


def test_averaging_a_shared_calibration_does_not_average_it_away():
    """`gold_aggregate` maps mean → avg and drops uncertainty entirely.

    Were it to naively combine scalars as independent, forty readings off
    one bath would appear to reduce a 0.5 degC offset to 0.08. The offset is
    systematic: averaging cannot touch it. This is the single most expensive
    mistake available here, because it manufactures confidence.
    """
    forty = [_reading(30.0 + i * 0.01, f"signals:tc-{i}:repeatability") for i in range(40)]
    got = mean(forty)

    assert got.value == pytest.approx(30.195, abs=1e-3)
    # the shared offset survives averaging, undiminished
    assert got.terms[BATH] == pytest.approx(0.5)
    naive_if_independent = 0.5 / math.sqrt(40)
    assert got.low > naive_if_independent * 5
    assert got.low == pytest.approx(math.sqrt(0.5**2 + (0.1 / math.sqrt(40)) ** 2), rel=1e-6)


# --- 2. Step-hold padding fabricates, and now says so ------------------


def test_step_hold_padding_mints_its_own_uncertainty():
    """Padding a slow channel onto a fast grid FABRICATES intermediate
    values. The platform already knows this and had no way to quantify it;
    a held sample now carries a symbol saying how much of it is invention.
    """
    measured = _reading(30.0, TC14_REP)
    held = Quantity(
        value=measured.value,
        unit="degC",
        terms={**measured.terms, STEP_HOLD: 0.8},
    )
    assert held.u > measured.u
    # And a series mixing measured and held samples is honest about which
    # part of its spread is fabrication rather than measurement.
    series = mean([measured, held, measured, held])
    assert STEP_HOLD in series.terms
    assert series.terms[STEP_HOLD] == pytest.approx(0.4)


# --- 3. The derived-channel double count -------------------------------


def test_a_derived_channel_cannot_double_count_its_own_inputs():
    """A log-mean temperature difference computed FROM thermocouples, then
    aggregated BESIDE them, double-counts. `derivation` ('raw' vs 'derived')
    was added to flag this and nothing consumed it. Carrying the source
    symbols makes the double count arithmetically impossible instead of
    merely documented.
    """
    hot = _reading(80.0, TC14_REP)
    cold = _reading(30.0, TC15_REP)
    dt = difference(hot, cold)  # the derived channel

    # dt is perfectly explained by its inputs — it introduces no new source.
    assert set(dt.terms) <= set(hot.terms) | set(cold.terms)
    # It is correlated with them — an independent rollup would miss that —
    # but only WEAKLY, and the weakness is the physics: the shared bath
    # offset cancelled out of the difference, so what remains in common is
    # just tc-14's own repeatability. A model that reported one scalar per
    # channel could express neither the correlation nor its smallness.
    r = correlation(dt, hot)
    assert 0.0 < abs(r) < 0.3

    rolled = add([hot, cold, dt])
    # The bath offset appears ONCE net, not three times: hot(+0.5) +
    # cold(+0.5) + dt(0.0, cancelled) — an independent treatment would have
    # claimed 3 × 0.5.
    assert rolled.terms[BATH] == pytest.approx(1.0)


# --- 4. A fault code is not a measurement ------------------------------


def test_a_quarantined_value_is_unquantified_not_zero_uncertainty():
    """Sixteen-bit fault codes were served as degrees. Per the fault
    taxonomy a bad reading's VALUE is NULL, so it contributes no magnitude
    and no uncertainty — and must be COUNTED, not dropped, or the mean looks
    better-supported than it is.
    """
    good = [_reading(30.0, f"signals:tc-{i}:repeatability") for i in range(3)]
    with_fault = add([*good, Unquantified(float("nan"), "degC")])
    assert with_fault.unquantified == 1
    assert with_fault.counted == 4
    said = with_fault.reads()
    assert "3 of 4" in said and "not in that range" in said


# --- 5. Unit conversion must move both numbers -------------------------


def test_w_to_mw_moves_the_uncertainty_with_the_value():
    """The conversion itself was verified correct; the risk is converting a
    value while leaving its uncertainty in the old unit, which is the
    two-columns-drift failure the silver schema deliberately avoids."""
    w = Quantity(value=1_250_000.0, unit="W", terms={"signals:pwr-1:cal": 25_000.0})
    mw = w.scaled(1e-6, unit="MW")
    assert (mw.value, mw.unit) == (pytest.approx(1.25), "MW")
    assert mw.u == pytest.approx(0.025)
    assert mw.u / mw.value == pytest.approx(w.u / w.value)  # relative uncertainty invariant


# --- 6. A model's prediction is not a measurement ----------------------


def test_a_surrogate_carries_its_own_error_and_says_where_it_stops_holding():
    """A ROM prediction inherits the uncertainty of its inputs AND adds its
    own. Outside the training domain the extrapolation term dominates — and
    the number there is not wrong, it is inapplicable, which the budget's
    `valid_over` states rather than leaving to the reader."""
    reg = Registry()
    reg.declare(
        Budget(
            symbol=ROM_ERR,
            measurand="reduced-order prediction of peak surface temperature",
            standard=4.0,
            kind="A",
            traceable_to="validation set, 2026-08",
            valid_over="power 0.4–1.0 MW, inlet 25–45 degC",
        )
    )
    reg.declare(
        Budget(
            symbol=ROM_EXTRAP,
            measurand="same, outside the validated envelope",
            standard=25.0,
            kind="B",
            traceable_to="unstated",
            valid_over="outside the envelope above; NOT a validated bound",
        )
    )
    inside = Quantity(value=410.0, unit="degC", terms={BATH: 0.5, ROM_ERR: 4.0})
    outside = Quantity(value=468.0, unit="degC", terms={BATH: 0.5, ROM_ERR: 4.0, ROM_EXTRAP: 25.0})

    assert outside.u > 6 * inside.u
    lines = reg.explain(outside)
    assert any("NOT a validated bound" in line for line in lines)
    # The measured input and the model error are separable, which is what a
    # single combined scalar would destroy.
    assert inside.terms[BATH] == pytest.approx(0.5)


# --- 7. Bounding, per the founder's call -------------------------------


def test_a_partially_characterised_channel_bounds_rather_than_refusing():
    """Real sites report uncertainty for some channels and not others. The
    call was to BOUND: assume independence for the floor and full
    correlation for the ceiling, and say the range is a range."""
    got = mean(
        [
            _reading(30.0, TC14_REP),
            MagnitudeOnly(30.2, "degC", 0.6),  # magnitude known, provenance not
            MagnitudeOnly(29.8, "degC", 0.4),
        ]
    )
    assert not got.exact
    assert got.low < got.high
    assert "between" in got.reads()
    # and the bound genuinely brackets both extremes
    structured = _reading(30.0, TC14_REP)
    assert got.low >= structured.u / 3 - 1e-9


def test_the_bound_collapses_to_a_number_once_provenance_is_declared():
    """The incentive has to point the right way: declaring where an
    uncertainty came from should TIGHTEN the answer, not merely document
    it."""
    loose = mean([MagnitudeOnly(30.0, "degC", 0.5), MagnitudeOnly(30.0, "degC", 0.5)])
    assert loose.low < loose.high

    declared = mean(
        [
            Quantity(30.0, "degC", {"signals:tc-a:noise": 0.5}),
            Quantity(30.0, "degC", {"signals:tc-b:noise": 0.5}),
        ]
    )
    assert declared.exact
    assert declared.low == pytest.approx(loose.low)  # the independent floor
    assert declared.low < loose.high
