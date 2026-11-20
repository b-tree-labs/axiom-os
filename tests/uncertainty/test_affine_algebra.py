# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The algebra has to close, or "infinitely composable" is a slogan."""

from __future__ import annotations

import math

import pytest

from axiom.uncertainty import (
    Budget,
    MagnitudeOnly,
    Quantity,
    Registry,
    SymbolError,
    Unquantified,
    add,
    check_symbol,
    correlation,
    difference,
    mean,
    product,
)

CAL = "signals:tc-14:calibration"
REP = "signals:tc-14:repeatability"
CAL2 = "signals:tc-15:calibration"


def _tc(value: float, *, cal: float = 0.5, rep: float = 0.1, sym: str = CAL) -> Quantity:
    return Quantity(value=value, unit="degC", terms={sym: cal, REP: rep})


def test_u_is_the_root_sum_of_squares():
    q = Quantity(value=30.0, unit="degC", terms={CAL: 0.3, REP: 0.4})
    assert q.u == pytest.approx(0.5)
    assert q.expanded(k=2) == pytest.approx(1.0)


def test_a_symbol_must_name_the_extension_that_owns_it():
    check_symbol("signals:tc-14:calibration")
    for bad in ("calibration", "signals:calibration", "Signals:tc:cal", ""):
        with pytest.raises(SymbolError):
            check_symbol(bad)


def test_shared_sources_correlate_without_anyone_declaring_it():
    """The whole reason for the affine form. Two readings off one bath are
    correlated because they SHARE a symbol, not because a matrix said so."""
    a = _tc(30.0)
    b = _tc(31.0)
    assert correlation(a, b) == pytest.approx(1.0)

    independent = _tc(31.0, sym=CAL2)
    assert 0.0 < correlation(a, independent) < 1.0


def test_a_shared_calibration_cancels_in_a_difference():
    """Two thermocouples on one bath differ MORE precisely than either is
    known absolutely. A scalar-only model cannot express this and would
    overstate the uncertainty of every temperature difference."""
    # Shared CALIBRATION (one bath), but repeatability is per-instrument:
    # sharing a symbol means sharing a physical source, and tc-14's noise is
    # not tc-15's. Getting this wrong is exactly the modelling error the
    # affine form forces into the open rather than hiding in a scalar.
    a = Quantity(value=30.0, unit="degC", terms={CAL: 0.5, REP: 0.1})
    b = Quantity(value=25.0, unit="degC", terms={CAL: 0.5, "signals:tc-15:repeatability": 0.1})
    d = difference(a, b)
    assert d.value == pytest.approx(5.0)
    # the calibration term subtracts out entirely
    assert CAL not in d.terms or d.terms[CAL] == pytest.approx(0.0)
    assert d.u == pytest.approx(math.sqrt(0.1**2 + 0.1**2))
    assert d.u < a.u


def test_independent_readings_combine_as_root_sum_square():
    xs = [Quantity(value=10.0, unit="degC", terms={f"signals:s{i}:noise": 0.2}) for i in range(4)]
    got = add(xs)
    assert got.exact
    assert got.low == pytest.approx(math.sqrt(4 * 0.2**2))


def test_a_shared_source_does_NOT_shrink_like_independent_ones():
    """Forty readings off one offset do not average the offset away. This is
    the error a scalar column invites and the reason for the whole design."""
    shared = [Quantity(value=10.0, unit="degC", terms={CAL: 0.5}) for _ in range(40)]
    assert mean(shared).low == pytest.approx(0.5)  # unchanged by averaging

    independent = [
        Quantity(value=10.0, unit="degC", terms={f"signals:s{i}:noise": 0.5}) for i in range(40)
    ]
    assert mean(independent).low == pytest.approx(0.5 / math.sqrt(40))


def test_composition_is_associative_and_order_free():
    """Closure, stated as a property: regrouping cannot change the answer."""
    xs = [_tc(float(i), cal=0.2 + i * 0.05, rep=0.1) for i in range(6)]
    whole = add(xs)
    halves = add(
        [
            Quantity(value=add(xs[:3]).value, unit="degC", terms=dict(add(xs[:3]).terms)),
            Quantity(value=add(xs[3:]).value, unit="degC", terms=dict(add(xs[3:]).terms)),
        ]
    )
    assert halves.value == pytest.approx(whole.value)
    assert halves.low == pytest.approx(whole.low)
    assert add(list(reversed(xs))).low == pytest.approx(whole.low)


def test_scaling_keeps_value_and_uncertainty_in_one_unit():
    """W→MW. The recorded defect class was a value converted while its
    uncertainty was not."""
    w = Quantity(value=2_000_000.0, unit="W", terms={"signals:pwr:cal": 50_000.0})
    mw = w.scaled(1e-6, unit="MW")
    assert mw.value == pytest.approx(2.0)
    assert mw.u == pytest.approx(0.05)
    assert mw.unit == "MW"


def test_a_nonlinear_product_tracks_what_the_linearisation_threw_away():
    p = product(
        Quantity(value=2.0, unit="m", terms={"signals:len:cal": 0.1}),
        Quantity(value=3.0, unit="m", terms={"signals:wid:cal": 0.2}),
        residual_symbol="data_platform:product:residual",
    )
    assert p.value == pytest.approx(6.0)
    assert "data_platform:product:residual" in p.terms
    assert p.terms["data_platform:product:residual"] == pytest.approx(0.1 * 0.2)


def test_mixed_units_are_refused_rather_than_summed():
    with pytest.raises(ValueError, match="mixed units"):
        add([Quantity(1.0, "degC", {}), Quantity(1.0, "MW", {})])


# --- Absence has kinds -------------------------------------------------


def test_magnitudes_without_structure_produce_a_bound_not_a_number():
    got = add([MagnitudeOnly(10.0, "degC", 0.3), MagnitudeOnly(10.0, "degC", 0.4)])
    assert not got.exact
    assert got.low == pytest.approx(0.5)  # independent
    assert got.high == pytest.approx(0.7)  # fully correlated
    assert "how they correlate is not" in got.reads()


def test_an_unreported_uncertainty_is_counted_and_left_OUTSIDE_the_bound():
    """Nothing bounds a quantity nobody characterised. Folding it in as
    zero would be a claim of perfect precision — the exact mistake the
    silver column's own comment warns about."""
    got = add(
        [
            Quantity(10.0, "degC", {CAL: 0.5}),
            Unquantified(10.0, "degC"),
        ]
    )
    assert got.unquantified == 1
    assert got.counted == 2
    assert got.low == pytest.approx(0.5)
    said = got.reads()
    assert "1 reported no uncertainty" in said
    assert "not in that range" in said


def test_when_nothing_reported_anything_no_range_is_claimed():
    got = add([Unquantified(1.0, "degC"), Unquantified(2.0, "degC")])
    assert "none is claimed" in got.reads()


def test_an_empty_term_map_is_a_claim_of_exactness_not_an_absence():
    exact = Quantity(value=273.15, unit="K", terms={})
    assert exact.u == 0.0
    assert add([exact, exact]).exact


# --- Context and relativity -------------------------------------------


def test_a_budget_without_its_measurand_is_refused():
    with pytest.raises(ValueError, match="measurand"):
        Budget(symbol=CAL, measurand="", standard=0.5)


def test_the_registry_explains_a_quantity_and_names_what_is_undeclared():
    reg = Registry()
    reg.declare(
        Budget(
            symbol=CAL,
            measurand="thermocouple tc-14 indicated temperature",
            standard=0.5,
            kind="B",
            traceable_to="NIST-traceable bath, cert 2026-03",
            valid_over="0–400 degC",
        )
    )
    q = _tc(30.0)
    lines = reg.explain(q)
    assert any("NIST-traceable bath" in line for line in lines)
    assert any("nothing declared it" in line for line in lines)
    assert reg.undeclared(q) == [REP]


def test_redeclaring_a_symbol_differently_is_refused():
    reg = Registry()
    reg.declare(Budget(symbol=CAL, measurand="x", standard=0.5))
    reg.declare(Budget(symbol=CAL, measurand="x", standard=0.5))  # idempotent
    with pytest.raises(SymbolError, match="already declared"):
        reg.declare(Budget(symbol=CAL, measurand="x", standard=0.9))


def test_composition_works_without_the_declaring_extension():
    """Resilience: a missing extension degrades what you can EXPLAIN, never
    what you can COMPUTE."""
    reg = Registry()  # nothing declared at all
    got = add([_tc(10.0), _tc(11.0)])
    assert got.low > 0
    assert len(reg.undeclared(got)) == 2
