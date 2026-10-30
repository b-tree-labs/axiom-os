# Copyright (c) 2026 B-Tree Ventures, LLC
# SPDX-License-Identifier: Apache-2.0
"""A line, a bar and a scatter are one chart drawn three ways.

Measured against a deployed analytics instance rather than a design document:
twenty charts, and every one is a time series or a table — thirteen lines, three
bars, two scatters, one mixed series with a second axis. No heatmap, no
histogram, no scatter of one measurement against another.

So the gap was never five chart kinds. It was the mark, which is a property of
the series and not of the spec: the same rows over the same time axis, drawn
differently. Registering three more kinds to say "line", "bar" and "scatter"
would have made the registry describe the renderer instead of the data.

What must survive every mark is the gap. A missing reading is missing however it
is drawn, and a bar or a dot at that instant asserts a measurement nobody took —
which is the failure this whole surface exists to prevent.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from axiom.extensions.builtins.scidisplay.chart_svg import MARKS, Series, render_svg

T0 = datetime(2026, 9, 25, tzinfo=UTC)


def _points(values):
    return [(T0 + timedelta(minutes=i), v) for i, v in enumerate(values)]


def _svg(mark, values=(1.0, 2.0, 3.0, 4.0), **kw):
    return render_svg(
        [Series(name="s", points=_points(values), unit="degC", mark=mark, **kw)],
        title="t")


class TestEveryMarkDraws:
    @pytest.mark.parametrize("mark", MARKS)
    def test_it_produces_a_document(self, mark):
        svg = _svg(mark)
        assert svg.startswith("<svg") and svg.rstrip().endswith("</svg>")

    def test_a_line_draws_a_path(self):
        assert "<polyline" in _svg("line")

    def test_a_bar_draws_one_rectangle_per_reading(self):
        # One extra rect is the paper.
        assert _svg("bar").count("<rect") == 4 + 1

    def test_a_scatter_draws_one_dot_per_reading(self):
        assert _svg("scatter").count("<circle") == 4

    def test_the_marks_really_differ(self):
        """Stated so a fallback that quietly drew a line for everything could
        not pass the tests above."""
        line, bar, scatter = (_svg(m) for m in ("line", "bar", "scatter"))
        assert "<polyline" in line and "<polyline" not in bar
        assert bar.count("<rect") > scatter.count("<rect")
        assert scatter.count("<circle") > line.count("<circle")


class TestAGapStaysAGapInEveryMark:
    """The property that must not depend on how it is drawn."""

    def test_a_line_breaks_rather_than_joining_across(self):
        svg = _svg("line", values=(1.0, 2.0, None, 4.0, 5.0))
        assert svg.count("<polyline") == 2

    def test_a_bar_is_not_drawn_for_a_missing_reading(self):
        with_gap = _svg("bar", values=(1.0, 2.0, None, 4.0))
        assert with_gap.count("<rect") == 3 + 1

    def test_a_dot_is_not_drawn_for_a_missing_reading(self):
        assert _svg("scatter", values=(1.0, None, 3.0)).count("<circle") == 2

    def test_an_all_null_series_draws_no_marks(self):
        for mark in MARKS:
            svg = _svg(mark, values=(None, None, None))
            assert svg.count("<circle") == 0
            assert svg.count("<polyline") == 0
            assert svg.count("<rect") == 1  # paper only


class TestTheMarkIsDeclaredNotGuessed:
    def test_the_default_is_a_line(self):
        assert Series(name="s", points=_points((1.0, 2.0))).mark == "line"

    def test_an_unknown_mark_falls_back_to_a_line(self):
        """Rather than drawing nothing. A chart that renders empty reads as a
        data problem and sends somebody to look in the wrong place."""
        assert "<polyline" in _svg("wiggle")

    def test_the_set_is_enumerated(self):
        assert MARKS == ("line", "bar", "scatter")


class TestMarksComposeWithWhatWasAlreadyThere:
    def test_a_modelled_series_still_reads_as_modelled(self):
        """Provenance is not a mark. A modelled bar is drawn more faintly, the
        way a modelled line is dashed, so which rows were measured survives the
        choice of mark."""
        measured = _svg("bar")
        modelled = _svg("bar", modelled=True)
        assert "fill-opacity" in modelled
        assert measured != modelled

    def test_several_series_of_bars_do_not_sit_on_top_of_each_other(self):
        svg = render_svg(
            [Series(name="a", points=_points((1.0, 2.0, 3.0)), unit="degC", mark="bar"),
             Series(name="b", points=_points((2.0, 3.0, 4.0)), unit="degC", mark="bar")],
            title="t")
        xs = [float(chunk.split('"')[0]) for chunk in svg.split('<rect x="')[1:]]
        assert len(set(xs)) == len(xs), "bars from two series must not overlap"
