# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""P2 — deterministic analytics tool (ADR-113 Tiers 1-3; spec-analytics-tool)."""
from __future__ import annotations

import json
import math

import pytest

from axiom.extensions.builtins.analytics.core import (
    AnalyticsError,
    OPS,
    compute,
    resolve_series,
)

S = [2.0, 4.0, 6.0]  # sum 12, mean 4, min 2, max 6


# --- Tier 1: descriptive ----------------------------------------------------
@pytest.mark.parametrize(
    "op,expected",
    [
        ("sum", 12.0), ("mean", 4.0), ("min", 2.0), ("max", 6.0),
        ("median", 4.0), ("range", 4.0), ("count", 3),
        ("std", math.sqrt(8.0 / 3.0)), ("var", 8.0 / 3.0),
    ],
)
def test_tier1_scalar(op, expected):
    r = compute(op, S)
    assert r["value"] == pytest.approx(expected)


def test_percentile():
    assert compute("percentile", S, params={"q": 50})["value"] == pytest.approx(4.0)


# --- Tier 2: trend / fit ----------------------------------------------------
def test_linregress_exact():
    r = compute("linregress", S)  # x defaults to [0,1,2]
    assert r["slope"] == pytest.approx(2.0)
    assert r["intercept"] == pytest.approx(2.0)
    assert r["r2"] == pytest.approx(1.0)


def test_polyfit_degree2_exact():
    r = compute("polyfit", S, params={"degree": 2})
    assert r["r2"] == pytest.approx(1.0)
    assert len(r["coeffs"]) == 3


def test_moving_average():
    assert compute("moving_average", S, params={"window": 2})["values"] == pytest.approx([3.0, 5.0])


def test_rate_of_change():
    r = compute("rate_of_change", S)
    assert r["mean_delta_per_step"] == pytest.approx(2.0)
    assert r["values"] == pytest.approx([2.0, 2.0])


def test_correlation():
    r = compute("correlation", S, params={"x": [1.0, 2.0, 3.0]})
    assert r["pearson_r"] == pytest.approx(1.0)


# --- Tier 3: calculus on samples -------------------------------------------
def test_cumsum():
    assert compute("cumsum", S)["values"] == pytest.approx([2.0, 6.0, 12.0])


def test_derivative():
    assert compute("derivative", S)["values"] == pytest.approx([2.0, 2.0, 2.0])


def test_integral_trapezoid():
    assert compute("integral", S)["value"] == pytest.approx(8.0)


def test_interpolate():
    assert compute("interpolate", S, params={"at": 1})["value"] == pytest.approx(4.0)


# --- Format-agnostic resolution --------------------------------------------
@pytest.mark.parametrize(
    "series,column",
    [
        ([2.0, 4.0, 6.0], None),
        (json.dumps([2.0, 4.0, 6.0]), None),
        (json.dumps([{"peak": 2.0}, {"peak": 4.0}, {"peak": 6.0}]), "peak"),
        ("peak\n2.0\n4.0\n6.0\n", "peak"),
        ("date,peak\n2026-09-01,2.0\n2026-09-02,4.0\n2026-09-03,6.0\n", "peak"),
        ("peaks: 2.0, 4.0, 6.0", None),
    ],
)
def test_format_agnostic_mean(series, column):
    assert compute("mean", series, column=column)["value"] == pytest.approx(4.0)
    assert resolve_series(series, column) == pytest.approx(S)


# --- Determinism, provenance stamp, empty, errors ---------------------------
def test_deterministic():
    a = compute("std", S)
    b = compute("std", S)
    assert a == b


def test_provenance_stamp_present():
    r = compute("mean", json.dumps([{"peak": 2.0}, {"peak": 4.0}, {"peak": 6.0}]),
                column="peak", source="reactor_daily")
    for k in ("op", "n", "params", "method", "source", "series"):
        assert k in r
    assert r["op"] == "mean" and r["n"] == 3 and r["source"] == "reactor_daily"
    assert r["method"] == "numpy.mean"


def test_empty_series_returns_null_not_fabrication():
    r = compute("mean", [])
    assert r["value"] is None and "reason" in r


def test_unknown_op_raises():
    with pytest.raises(AnalyticsError):
        compute("solve_ode", S)


def test_ops_catalog_covers_all_tiers():
    for op in ("sum", "mean", "std", "linregress", "polyfit", "derivative", "integral", "cumsum"):
        assert op in OPS


# --- Skill wrapper (ADR-056: run(params, ctx) -> SkillResult) --------------
from axiom.extensions.builtins.analytics.skills.compute import run  # noqa: E402


def test_skill_ok_with_stamped_result():
    res = run({"op": "mean", "series": [2.0, 4.0, 6.0], "source": "reactor_daily"})
    assert res.ok
    r = res.value["result"]
    assert r["value"] == pytest.approx(4.0) and r["source"] == "reactor_daily"


def test_skill_missing_op():
    assert not run({"series": [1.0, 2.0]}).ok


def test_skill_missing_series():
    assert not run({"op": "mean"}).ok


def test_skill_unknown_op():
    res = run({"op": "solve_pde", "series": [1.0, 2.0]})
    assert not res.ok and res.errors


# --- a named column that does not exist must FAIL, never substitute ----------
#
# This is the worst failure this module can have: answering the question the
# caller did not ask, with a real-looking number. A typo'd or renamed column
# silently became "the first numeric column", so `mean(peak)` could return the
# mean of an id column and nothing anywhere would say so.


def test_a_missing_csv_column_refuses_instead_of_using_another_one():
    with pytest.raises(AnalyticsError) as e:
        resolve_series("a,b\n1,2\n3,4", "zz")
    assert "zz" in str(e.value)


def test_negative_control_a_present_csv_column_still_resolves():
    # Proves the guard above is not vacuously green.
    assert resolve_series("a,b\n1,2\n3,4", "b") == [2.0, 4.0]


def test_a_missing_dict_key_refuses_instead_of_picking_another_series():
    with pytest.raises(AnalyticsError):
        resolve_series({"peak": [1.0, 2.0], "other": [9.0]}, "nope")


def test_negative_control_a_present_dict_key_still_resolves():
    assert resolve_series({"peak": [1.0, 2.0], "other": [9.0]}, "peak") == [1.0, 2.0]


def test_an_out_of_range_column_index_refuses():
    with pytest.raises(AnalyticsError):
        resolve_series("a,b\n1,2\n3,4", 9)


def test_the_skill_surfaces_the_refusal_rather_than_a_number():
    from axiom.extensions.builtins.analytics.skills.compute import run

    r = run({"op": "mean", "series": "date,peak\n2026-01-01,10\n2026-01-02,20", "column": "Peak"})
    assert r.ok is False, f"wrong-case column returned a number: {r.value}"
    assert any("Peak" in e for e in r.errors)
