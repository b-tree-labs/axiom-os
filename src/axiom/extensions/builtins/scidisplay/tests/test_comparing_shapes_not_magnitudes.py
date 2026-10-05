# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Two channels that move together, one reading 950 and the other 21.

Drawn as they are, the small one is a flat line along the bottom and the
figure is about the large one. Rebasing is the standard answer, and it also
unlocks the comparison a third unit blocks: a figure may carry two units and
no more, and rebased there is only one.
"""

from __future__ import annotations

import datetime as dt

import pytest

from axiom.extensions.builtins.scidisplay.chart_rebase import (
    PERCENT_CHANGE,
    rebase,
)
from axiom.extensions.builtins.scidisplay.chart_svg import Series, render_svg

T0 = dt.datetime(2026, 9, 1, tzinfo=dt.UTC)


def _s(name, values, unit="W", **kw):
    return Series(
        name, [(T0 + dt.timedelta(minutes=i), v) for i, v in enumerate(values)],
        unit=unit, **kw,
    )


class TestEverySeriesStartsAtZero:
    def test_the_first_reading_becomes_zero_percent(self):
        got = rebase([_s("power", [950.0, 1000.0])])
        assert got.series[0].points[0][1] == 0.0

    def test_and_the_rest_are_its_change(self):
        got = rebase([_s("power", [950.0, 1045.0])])
        assert round(got.series[0].points[1][1], 2) == 10.0

    def test_two_series_of_different_size_become_comparable(self):
        """The whole point: 950 -> 1045 and 21 -> 23.1 are the same move."""
        got = rebase([_s("power", [950.0, 1045.0]), _s("temp", [21.0, 23.1], unit="degC")])
        assert [round(s.points[1][1], 1) for s in got.series] == [10.0, 10.0]

    def test_they_share_one_unit_and_therefore_one_axis(self):
        got = rebase([_s("power", [950.0, 1045.0]), _s("temp", [21.0, 23.1], unit="degC")])
        assert {s.unit for s in got.series} == {"%"}

    def test_a_gap_is_still_a_gap(self):
        got = rebase([_s("power", [950.0, None, 1045.0])])
        assert got.series[0].points[1][1] is None


class TestEachFromItsOwnStart:
    def test_not_from_a_shared_instant(self):
        """Two channels that began recording at different times have no common
        first reading, and holding one to the other's would make a figure whose
        baseline is a moment one of them was not being read at."""
        late = Series(
            "late",
            [(T0 + dt.timedelta(minutes=5 + i), v) for i, v in enumerate([100.0, 110.0])],
            unit="W",
        )
        got = rebase([_s("early", [50.0, 55.0]), late])
        assert [s.points[0][1] for s in got.series] == [0.0, 0.0]


class TestWhatItRefuses:
    def test_a_series_that_starts_at_zero(self):
        """Change FROM zero is not a percentage of anything, and dividing by
        it emits an infinity or a number the size of a rounding error."""
        got = rebase([_s("cold", [0.0, 5.0])])
        assert not got.series
        assert got.refused[0][0] == "cold"
        assert "zero" in got.refused[0][1]

    def test_a_series_with_no_readings_here(self):
        got = rebase([_s("absent", [None, None])])
        assert got.refused[0][0] == "absent"

    def test_and_names_them_rather_than_dropping_them_quietly(self):
        got = rebase([_s("good", [1.0, 2.0]), _s("cold", [0.0, 5.0])])
        assert len(got.series) == 1
        assert [n for n, _why in got.refused] == ["cold"]

    def test_an_unknown_mode_is_refused_by_name(self):
        with pytest.raises(ValueError, match="unknown rebase mode"):
            rebase([_s("a", [1.0])], mode="sideways")


class TestTheReferenceTravelsWithTheNumber:
    """A bare percent passes every unit check and still means nothing without
    saying "of what". That is a rule this codebase already has."""

    def test_rebasing_returns_the_sentence(self):
        got = rebase([_s("power", [950.0, 1045.0])])
        assert "change from" in got.reference
        assert "2026-09-01" in got.reference

    def test_there_is_none_when_nothing_was_rebased(self):
        assert rebase([_s("cold", [0.0])]).reference == ""

    def test_a_figure_can_carry_it(self):
        got = rebase([_s("power", [950.0, 1045.0]), _s("temp", [21.0, 23.1], unit="degC")])
        svg = render_svg(list(got.series), title="t", subtitle=got.reference)
        assert "change from" in svg
        assert "%" in svg


class TestTheModeIsNamed:
    def test_percent_change_is_the_default(self):
        assert rebase([_s("a", [2.0, 3.0])]).series == rebase(
            [_s("a", [2.0, 3.0])], mode=PERCENT_CHANGE
        ).series
