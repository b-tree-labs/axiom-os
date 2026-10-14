# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A figure a researcher would put in a paper.

Every assertion here corresponds to a defect found by RENDERING the figure
and looking at it. Each one shipped as valid SVG and was unusable, which is
the only kind of bug a chart has.
"""

from __future__ import annotations

import datetime as dt
import re

import pytest

from axiom.extensions.builtins.scidisplay.chart_svg import (
    COLUMN_MM,
    Geometry,
    Series,
    flatten_theme,
    render_svg,
    scale_for,
)

T0 = dt.datetime(2026, 9, 20, 14, 0, tzinfo=dt.UTC)


def _left_axis_unit(svg: str) -> str:
    """What the LEFT axis says it is measured in."""
    m = re.search(r'text-anchor="end"[^>]*letter-spacing="0\.05em">([^<]*)</text>', svg)
    return m.group(1) if m else ""


def _ramp(n=60, base=28.0, unit="degC", step=3):
    return Series(
        "TC-CP1_1",
        [(T0 + dt.timedelta(seconds=i * step), base + i) for i in range(n)],
        unit=unit,
    )


class TestTheAxisSaysNumbersAPersonWouldSay:
    def test_a_megawatt_reactor_is_labelled_in_megawatts(self):
        """`1000000` makes the reader do arithmetic to learn something the
        axis already knew."""
        assert scale_for(1_000_000, "W") == (1e6, "MW")

    def test_a_prefix_is_not_glued_to_a_unit_that_takes_none(self):
        """"kpct" is not a unit anybody uses. The test is the unit, not the
        size of the number."""
        assert scale_for(50_000, "%")[1] == "%"
        assert scale_for(50_000, "degC")[1] == "degC"

    def test_small_values_scale_down_too(self):
        assert scale_for(0.004, "A") == (1e-3, "mA")


class TestItSurvivesAJournalColumn:
    @pytest.mark.parametrize("column", sorted(COLUMN_MM))
    def test_every_preset_renders(self, column):
        assert render_svg([_ramp()], title="t",
                          geometry=Geometry.for_column(column)).startswith("<svg")

    def test_a_narrow_figure_gets_smaller_type_not_larger(self):
        """The first cut had this inverted, and produced a 336-unit canvas
        carrying 33-unit text: title clipped off the edge, ticks
        overlapping into a blob, the plot squashed to a sliver."""
        single = Geometry.for_column("single")
        screen = Geometry.for_column("screen")
        assert single.scale < screen.scale

    def test_type_stays_above_the_legibility_floor(self):
        """Sub-linear, so a single-column figure keeps readable type rather
        than being the screen figure shrunk."""
        single = Geometry.for_column("single")
        title_points = 19 * single.scale * 72 / 96
        assert 7.5 <= title_points <= 11, f"{title_points:.1f}pt"

    def test_an_unknown_column_says_what_there_is(self):
        with pytest.raises(ValueError, match="single"):
            Geometry.for_column("A4")


class TestNothingCollides:
    def _svg(self, **kw):
        return render_svg(
            [_ramp()], title="A title", subtitle="A subtitle",
            provenance="silver.signals · site", **kw
        )

    @pytest.mark.parametrize("column", sorted(COLUMN_MM))
    def test_the_plot_has_room_left_after_the_furniture(self, column):
        """Margins are computed from the CONTENT. Scaled from a constant
        they fitted one preset and collided at every other size."""
        geo = Geometry.for_column(column)
        self._svg(geometry=geo)
        assert geo.height - geo.top - geo.bottom > geo.height * 0.4

    def test_a_subtitle_earns_its_own_space(self):
        with_sub = Geometry.for_column("single")
        render_svg([_ramp()], title="t", subtitle="s", geometry=with_sub)
        without = Geometry.for_column("single")
        render_svg([_ramp()], title="t", geometry=without)
        assert with_sub.top > without.top


class TestItNeverOverlaysTwoUnitsOnOneAxis:
    """A temperature read against a power axis is the most confidently wrong
    thing this renderer could do. The rule is about one LABELLED axis, though,
    not about one unit: two units with two labelled axes read correctly, and
    that figure is now drawn without being asked for.
    """

    def _mixed(self):
        power = Series(
            "A1:CORE:PWR",
            [(T0 + dt.timedelta(seconds=i * 3), 900_000.0) for i in range(60)],
            unit="W",
        )
        return [power, _ramp()]

    def test_two_units_get_two_axes_and_both_are_drawn(self):
        svg = render_svg(self._mixed(), title="t")
        assert svg.count("<polyline") == 2
        assert "A1:CORE:PWR" in svg and "TC-CP1_1" in svg

    def test_a_third_unit_is_refused_and_named(self):
        """Nobody holds three scales, so the third is left out — by NAME, not
        by unit. "not drawn: bar" describes the mechanism; a reader who picked
        four channels and sees three lines needs to know which one is
        missing."""
        third = Series("PT-1", [(T0 + dt.timedelta(seconds=i), 3.0) for i in range(20)],
                       unit="bar")
        svg = render_svg([*self._mixed(), third], title="t")
        assert svg.count("<polyline") == 2
        assert "not drawn: PT-1" in svg

    def test_a_single_unit_draws_everything(self):
        svg = render_svg([_ramp(), _ramp()], title="t")
        assert svg.count("TC-CP1_1") >= 2


class TestTheColourCodeIsTheSameInEveryFigure:
    def test_a_model_takes_the_colour_of_what_it_models(self):
        """One reserved colour said "this is a model" and lost WHICH model.
        Hue carries the quantity; the dash and the word carry the provenance."""
        measured = _ramp()
        svg = render_svg(
            [measured,
             Series(measured.name, measured.points, unit="degC",
                    modelled=True, against=measured.name)],
            title="t",
        )
        drawn = re.findall(
            r'<polyline points="[^"]*" fill="none" stroke="(#[0-9a-f]{6})"', svg
        )
        assert len(drawn) == 2 and drawn[0] == drawn[1]

    def test_a_model_is_dashed_as_well_as_coloured(self):
        """Colour alone fails in greyscale, which is how a figure is often
        printed — so where a measurement and its model share a plot, the
        model is dashed as well as coloured."""
        svg = render_svg(
            [
                Series("tc", _ramp().points, unit="degC"),
                Series("ROM", _ramp().points, unit="degC", modelled=True),
            ],
            title="t",
        )
        assert "stroke-dasharray" in svg

    def test_but_a_plot_of_nothing_but_models_is_drawn_solid(self):
        """The dash is a CONTRAST. With no measurement on the plot it
        separates nothing, and every line pays the legibility for it.

        Ben, on a figure of two predictions: "the dotted line for the plots
        doesn't look as good as it could look without the dotted line."
        Provenance is not lost — "(model)" is still in each label, and a word
        survives a photocopy better than a dash pattern does."""
        svg = render_svg(
            [
                Series("corrected", _ramp().points, unit="degC", modelled=True),
                Series("predicted", _ramp().points, unit="degC", modelled=True),
            ],
            title="t",
        )
        assert "stroke-dasharray" not in svg
        assert "(model)" in svg


class TestItDrawsOnlyWhatWasMeasured:
    def test_a_gap_breaks_the_line(self):
        """Joining across a gap draws a reading nobody took."""
        points = [
            (T0 + dt.timedelta(seconds=i * 3), None if 20 <= i < 30 else float(i))
            for i in range(60)
        ]
        svg = render_svg([Series("tc", points, unit="degC")], title="t")
        assert svg.count("<polyline") >= 2, "the gap did not break the path"

    def test_no_readings_says_so_rather_than_drawing_axes(self):
        svg = render_svg([Series("tc", [(T0, None)], unit="degC")], title="t")
        assert "no readings to draw" in svg


class TestALabelReadsAsOneThing:
    """A label is a name and its reading. Stacked on two lines they sat as
    close to the NEXT series' name as to their own, so the reader had to
    work out which line a number belonged to — the one thing a direct
    label exists to remove.
    """

    def _svg(self):
        return render_svg(
            [_ramp(), Series("ROM", _ramp().points, unit="degC", modelled=True)],
            title="t", geometry=Geometry.for_column("single"),
        )

    def test_the_value_shares_its_name_s_line(self):
        svg = self._svg()
        assert "<tspan" in svg, "the reading is not on the name's line"

    def test_the_value_is_separated_without_whitespace(self):
        """SVG collapses leading spaces, which ran the value straight into
        the name as "ROM (model)238"."""
        svg = self._svg()
        assert "dx=" in svg

    def test_the_value_is_subordinate_to_the_name(self):
        """Distance alone cannot group them when both are the same weight
        and size."""
        svg = self._svg()
        assert 'font-weight="400"' in svg
        assert 'font-weight="600"' in svg

    def test_the_margin_leaves_room_for_both(self):
        """A margin measured from the name alone clipped the reading off
        the edge."""
        geo = Geometry.for_column("single")
        render_svg([_ramp()], title="t", geometry=geo)
        assert geo.right > len("TC-CP1_1") * 7


class TestTheAxisSaysWhichDayItIs:
    """A window inside one afternoon used to name no date anywhere.

    The axis read `14:06 · 15:00 · 16:00 · 17:00 · 18:00` and the only place
    the day appeared was inside the ISO timestamps of the provenance line, in
    8-point grey. Learning which afternoon you were looking at meant parsing
    `2026-07-24T18:32:46.373000+00:00`.
    """

    def _svg(self, start, span, points=40):
        step = span / points
        s = Series(
            "PTC1",
            [(start + dt.timedelta(seconds=i * step.total_seconds()), 20.0 + i)
             for i in range(points + 1)],
            unit="degC",
        )
        return render_svg([s], title="loop")

    def test_a_window_inside_one_day_still_names_the_day(self):
        svg = self._svg(dt.datetime(2026, 7, 24, 14, 6, tzinfo=dt.UTC), dt.timedelta(hours=4))
        assert "24 Jul" in svg, "the axis never says which day this is"

    def test_it_names_the_day_once_not_at_every_tick(self):
        svg = self._svg(dt.datetime(2026, 7, 24, 14, 6, tzinfo=dt.UTC), dt.timedelta(hours=4))
        assert svg.count("24 Jul") == 1, "the same date under every tick is noise"

    def test_a_window_that_crosses_midnight_names_both_ends(self):
        svg = self._svg(dt.datetime(2026, 7, 23, 18, 0, tzinfo=dt.UTC), dt.timedelta(hours=20))
        assert "23 Jul" in svg and "24 Jul" in svg, (
            "a window spanning two days must say which end is which"
        )

    def test_the_last_tick_carries_the_closing_date(self):
        """Not just the tick where the day happens to turn over.

        A 30-hour window ticked every six hours turns the day over at its
        fifth tick and then runs on. Dating only the turnover leaves the
        right-hand end of the axis undated, which is half of what was asked
        for.
        """
        from axiom.extensions.builtins.scidisplay.chart_svg import (
            _time_labels,
            _time_ticks,
        )

        start = dt.datetime(2026, 7, 23, 18, 0, tzinfo=dt.UTC)
        ticks, step = _time_ticks(start, start + dt.timedelta(hours=26))
        labels = _time_labels(ticks, step)
        assert ticks[-1].date() == ticks[-2].date(), "pick a window that does not end on a day boundary"
        assert labels[-1][1] == "24 Jul", f"the closing tick is undated: {labels[-1]}"

    def test_day_scale_labels_carry_the_year_at_the_ends(self):
        """`17 Jul · 24 Jul` does not say which year, and cannot."""
        svg = self._svg(dt.datetime(2026, 7, 17, tzinfo=dt.UTC), dt.timedelta(days=7))
        assert "2026" in svg

    def test_a_label_that_already_says_the_year_is_not_given_another(self):
        svg = self._svg(dt.datetime(2025, 1, 1, tzinfo=dt.UTC), dt.timedelta(days=900))
        years = re.findall(r">(\d{4})</tspan>", svg)
        assert years == [], f"the year is already in the label: {years}"


class TestAnAbsenceJoinsTheAxisRatherThanBeingExcludedByIt:
    """`measured_cm` declares no unit; `predicted_cm` declares cm. Asked to
    draw the measurement against the model of it, the figure drew ONE LINE.

    It was wrong twice, in opposite directions. First the axis went to
    whichever series was drawn first, so the undeclared one took it and the
    declared one was dropped. Then the declared one took it and the undeclared
    ones were dropped, which is a third of what a reader asked for.

    An undeclared unit is not a claim that the quantity is different. It is
    the absence of any claim, and the reader asking for the comparison is the
    one making the case. So they are drawn, on the one declared axis, and
    NAMED as unverified.
    """

    def _pair(self, first_unit, second_unit):
        return [
            Series("measured_cm",
                   [(T0 + dt.timedelta(minutes=i), 500.0 + i) for i in range(20)],
                   unit=first_unit),
            Series("predicted_cm",
                   [(T0 + dt.timedelta(minutes=i), 505.0 + i) for i in range(20)],
                   unit=second_unit),
        ]

    def test_both_are_drawn(self):
        assert render_svg(self._pair("", "cm"), title="t").count("<polyline") == 2

    def test_the_declared_unit_takes_the_axis(self):
        """Even though the undeclared one is returned first, which is the
        order the node returns them in."""
        svg = render_svg(self._pair("", "cm"), title="t")
        assert _left_axis_unit(svg) == "cm", _left_axis_unit(svg)

    def test_the_undeclared_one_is_named_as_unverified(self):
        """Showing them without a word implies the axis was checked for them.
        It was not, and the figure says which ones."""
        svg = render_svg(self._pair("", "cm"), title="t")
        assert "measured_cm declare no unit" in svg
        assert "unverified" in svg
        assert "channel map" in svg

    def test_nothing_is_said_when_every_series_declares_one(self):
        svg = render_svg(self._pair("cm", "cm"), title="t")
        assert "unverified" not in svg

    def test_two_declared_units_leave_the_undeclared_nowhere_to_join(self):
        """With one axis the absence joins it. With two there is no single
        axis to join, and guessing which would be the confident wrongness the
        whole rule exists to prevent."""
        series = [
            Series("T", [(T0, 20.0), (T0 + dt.timedelta(minutes=1), 21.0)], unit="degC"),
            Series("P", [(T0, 900.0), (T0 + dt.timedelta(minutes=1), 950.0)], unit="W"),
            Series("mystery", [(T0, 5.0), (T0 + dt.timedelta(minutes=1), 6.0)], unit=""),
        ]
        svg = render_svg(series, title="t")
        assert svg.count("<polyline") == 2
        assert "not drawn: mystery" in svg

    def test_the_unit_the_most_series_share_leads(self):
        series = [
            Series("T1", [(T0, 20.0), (T0 + dt.timedelta(minutes=1), 21.0)], unit="degC"),
            Series("P", [(T0, 900.0), (T0 + dt.timedelta(minutes=1), 950.0)], unit="W"),
            Series("T2", [(T0, 22.0), (T0 + dt.timedelta(minutes=1), 23.0)], unit="degC"),
        ]
        svg = render_svg(series, title="t")
        assert _left_axis_unit(svg) == "degC", _left_axis_unit(svg)
        assert svg.count("<polyline") == 3, "the second unit should have the right axis"


class TestASingleReadingIsAMarkNotNothing:
    """Two ways a series vanished while the figure said it had drawn it.

    A series whose whole span falls inside one bucket of a much wider window —
    which is what a comparison of things recorded weeks apart looks like —
    became one point, and one point is not a line. And an isolated reading
    between two gaps was dropped outright, because only runs of two or more
    were ever emitted.

    Both read as "this series is not here", which is the one thing it is not.
    """

    def test_a_series_of_one_reading_is_drawn(self):
        svg = render_svg(
            [Series("lonely", [(T0, 21.0)], unit="degC"),
             Series("busy", [(T0 + dt.timedelta(minutes=i), 20.0 + i) for i in range(30)],
                    unit="degC")],
            title="t",
        )
        assert svg.count("<circle") >= 1, "the single reading is not on the page"
        assert svg.count("<polyline") == 1

    def test_an_isolated_reading_between_gaps_is_drawn(self):
        points = [
            (T0, 20.0), (T0 + dt.timedelta(minutes=1), 21.0),
            (T0 + dt.timedelta(minutes=2), None),
            (T0 + dt.timedelta(minutes=3), 25.0),          # alone between gaps
            (T0 + dt.timedelta(minutes=4), None),
            (T0 + dt.timedelta(minutes=5), 22.0), (T0 + dt.timedelta(minutes=6), 23.0),
        ]
        svg = render_svg([Series("ragged", points, unit="degC")], title="t")
        assert svg.count("<polyline") == 2, "the two runs of two"
        assert svg.count("<circle") == 1, "the reading between them was dropped"

    def test_the_mark_is_big_enough_to_see(self):
        """2.6 pixels on a 920-pixel canvas is a speck, and a speck is what a
        reader reads as an empty chart."""
        svg = render_svg([Series("lonely", [(T0, 21.0)], unit="degC")], title="t")
        radius = float(re.search(r'<circle[^>]*r="([\d.]+)"', svg).group(1))
        assert radius >= 3.0, radius

    def test_a_gap_is_still_not_joined_across(self):
        """The rule this sits inside: joining across a gap draws a reading
        nobody took."""
        points = [(T0, 20.0), (T0 + dt.timedelta(minutes=1), 21.0),
                  (T0 + dt.timedelta(minutes=2), None),
                  (T0 + dt.timedelta(minutes=3), 25.0),
                  (T0 + dt.timedelta(minutes=4), 26.0)]
        svg = render_svg([Series("split", points, unit="degC")], title="t")
        assert svg.count("<polyline") == 2


class TestAnAxisCanBeHeldStill:
    """A sweep through time re-scales the y axis on every step unless it is
    told not to, and the trace jumps vertically while the reader is following
    it move sideways. What they are watching stops being the data."""

    def _fig(self, held=None):
        s = Series(
            "T",
            [(T0 + dt.timedelta(minutes=i), 20.0 + i * 0.1) for i in range(30)],
            unit="degC",
        )
        return render_svg([s], title="t", held_extent=held)

    def _ticks(self, svg):
        return re.findall(r'text-anchor="end"[^>]*>([\d.]+)</text>', svg)

    def test_by_default_the_window_decides(self):
        assert self._ticks(self._fig())[0] == "20"

    def test_a_held_range_wins(self):
        assert self._ticks(self._fig({"degC": (0.0, 100.0)})) == ["0", "50", "100"]

    def test_and_is_not_widened_to_fit_this_window(self):
        """The caller measured it over a span this window is a PART of, so
        widening it to these readings would be widening it to a subset."""
        held = self._fig({"degC": (0.0, 100.0)})
        assert "150" not in self._ticks(held)

    def test_a_unit_that_was_not_held_is_unaffected(self):
        both = render_svg(
            [Series("T", [(T0, 20.0), (T0 + dt.timedelta(minutes=1), 21.0)], unit="degC"),
             Series("P", [(T0, 900.0), (T0 + dt.timedelta(minutes=1), 950.0)], unit="W")],
            title="t", held_extent={"degC": (0.0, 100.0)},
        )
        assert "900" in both or "0.9" in both


class TestLabelsStayStillWhileTheDataMoves:
    """Ben, watching a sweep: "the labels themselves should stay in the same
    place ... it's a little disconcerting to see the labels move up and down.
    You don't want to be trying to chase those with your eye."

    A held axis is the signal that a sweep is running — the caller pins the
    range precisely so the picture holds still. The labels are part of the
    picture. Only the leader line, which the eye follows rather than tracks,
    moves with the data.
    """

    def _labelled(self, svg: str) -> list[tuple[float, str]]:
        """(y, name) for each direct label."""
        out = []
        # A direct label carries its reading in a tspan. The title is bold in
        # the same face and would otherwise count as a third label.
        for match in re.finditer(
            r'<text x="[\d.]+" y="([\d.]+)"[^>]*font-weight="600">([^<]*)<tspan',
            svg,
        ):
            out.append((float(match.group(1)), match.group(2)))
        return out

    def _two_windows(self, *, held):
        """The same two series, drawn over two different slices of time."""
        base = T0
        rising = [(base + dt.timedelta(seconds=i * 60), 20.0 + i) for i in range(40)]
        falling = [(base + dt.timedelta(seconds=i * 60), 60.0 - i) for i in range(40)]
        extent = {"degC": (0.0, 100.0)} if held else None
        out = []
        for lo, hi in ((0, 12), (26, 40)):
            out.append(
                render_svg(
                    [
                        Series(name="rising", unit="degC", points=rising[lo:hi]),
                        Series(name="falling", unit="degC", points=falling[lo:hi]),
                    ],
                    title="sweep",
                    held_extent=extent,
                )
            )
        return out

    def test_a_held_axis_holds_the_labels_too(self):
        first, last = self._two_windows(held=True)
        assert self._labelled(first) == self._labelled(last), (
            "the labels moved between two frames of a sweep — that is the "
            "bobbing a reader has to chase"
        )

    def test_and_they_sit_around_the_middle_of_the_plot(self):
        svg, _ = self._two_windows(held=True)
        ys = [y for y, _ in self._labelled(svg)]
        assert len(ys) == 2
        plot = re.search(r'data-plot="([\d.]+) ([\d.]+) ([\d.]+) ([\d.]+)"', svg)
        assert plot is not None
        top, height = float(plot.group(2)), float(plot.group(4))
        middle = top + height / 2
        assert abs(sum(ys) / len(ys) - middle) < height * 0.1

    def test_the_leader_lines_are_what_move(self):
        first, last = self._two_windows(held=True)
        def leaders(svg):
            return re.findall(r'<line x1="([\d.]+)" y1="([\d.]+)"', svg)

        assert leaders(first) != leaders(last), (
            "nothing moved at all — the labels are only still because the "
            "figure is"
        )

    def test_a_still_figure_still_labels_beside_its_own_line(self):
        # Unheld, a label belongs next to the line it names: that is the whole
        # reason to label directly rather than in a legend.
        first, last = self._two_windows(held=False)
        assert self._labelled(first) != self._labelled(last)


class TestAFigureFollowsThePageWithoutDependingOnOne:
    """Ben: "you're not honoring the dark theme when dark is the mode that
    the app is in."

    The renderer draws once, on a server, for a page it has never seen — and
    the same bytes are downloaded, dropped into a slide, and printed. A
    figure that picked a theme at render time would be wrong for one of
    those. `var(--name, #literal)` is right for all of them: the page
    resolves it if there is a page, and the literal stands if there is not.
    """

    def _figure(self) -> str:
        return render_svg(
            [Series(name="Power", unit="W",
                    points=[(T0 + dt.timedelta(seconds=i), 900.0 + i) for i in range(20)])],
            title="a figure",
        )

    def test_the_chrome_offers_the_page_a_say(self):
        svg = self._figure()
        for variable in (
            "--axk-plot-paper",
            "--axk-plot-ink",
            "--axk-plot-rule",
            "--axk-plot-muted",
        ):
            assert variable in svg, f"{variable} is not offered, so the page cannot theme it"

    def test_and_every_one_carries_the_literal_it_falls_back_to(self):
        # A `var()` with no fallback renders as nothing at all where there is
        # no page — which is an invisible figure in a paper.
        svg = self._figure()
        for match in re.finditer(r"var\((--[a-z-]+)(,?)([^)]*)\)", svg):
            assert match.group(2) == ",", f"{match.group(1)} has no fallback"
            assert re.fullmatch(r"#[0-9a-f]{6}", match.group(3)), match.group(0)

    def test_the_paper_is_still_white_where_nothing_answers(self):
        assert "var(--axk-plot-paper,#ffffff)" in self._figure()

    def test_a_series_colour_does_NOT_move_with_the_page(self):
        # A hue here says what the line MEASURES. One that changed with the
        # page would be a different claim about the data on a dark screen
        # than on a light one.
        svg = self._figure()
        drawn = re.findall(r"<polyline [^>]*stroke=\"([^\"]+)\"", svg)
        assert drawn, "nothing was drawn"
        assert all(c.startswith("#") for c in drawn), drawn

    def test_the_bytes_do_not_depend_on_who_is_looking(self):
        assert self._figure() == self._figure()


class TestAFigureThatIsLeavingTheBrowser:
    """Researchers put these in presentations and in Overleaf documents, so
    a figure has to survive leaving the page it was drawn in.

    A browser resolves `var()`. Almost nothing else does — cairosvg RAISES
    on one, and the Inkscape/LaTeX toolchain is inconsistent about custom
    properties in presentation attributes. So anything on its way out gets
    flattened to the literals, which are the light figure we mean to publish
    anyway.
    """

    def _figure(self) -> str:
        return render_svg(
            [Series(name="Power", unit="W",
                    points=[(T0 + dt.timedelta(seconds=i), 900.0 + i) for i in range(20)]),
             Series(name="FuelTemp", unit="degC",
                    points=[(T0 + dt.timedelta(seconds=i), 24.0 + i) for i in range(20)])],
            title="a figure", provenance="somewhere - gold.signals",
        )

    def test_nothing_themed_survives(self):
        out = flatten_theme(self._figure())
        assert "var(" not in out
        assert "--axk-plot" not in out

    def test_and_what_is_left_is_the_literal_that_was_offered(self):
        out = flatten_theme(self._figure())
        assert 'fill="#ffffff"' in out
        assert "#1a1a1a" in out

    def test_it_changes_nothing_else_at_all(self):
        drawn = self._figure()
        # Same document, minus the indirection: every non-colour byte is
        # where it was, so a flattened figure is the same picture.
        assert len(flatten_theme(drawn)) < len(drawn)
        assert flatten_theme(flatten_theme(drawn)) == flatten_theme(drawn)
        for fragment in ("<polyline", "a figure", "gold.signals", 'viewBox="'):
            assert fragment in flatten_theme(drawn)

    def test_a_converter_can_actually_read_it(self):
        # The point of all of the above. Not a claim about cairosvg — it is
        # the nearest stand-in we have for the toolchain a reader will use,
        # and it fails on the unflattened figure in exactly the way theirs
        # would.
        cairosvg = pytest.importorskip("cairosvg")
        drawn = self._figure()
        with pytest.raises(Exception):
            cairosvg.svg2pdf(bytestring=drawn.encode())
        pdf = cairosvg.svg2pdf(bytestring=flatten_theme(drawn).encode())
        assert pdf.startswith(b"%PDF")


class TestNothingIsDrawnOutsideThePlot:
    """Ben, on a sweep: "we're plotting lines off of the graph bounds."

    A reading outside the axis range has to go somewhere, and without a clip
    it goes wherever the arithmetic puts it — across the footer, over the
    direct labels, off the canvas entirely.

    It became visible the moment the held axis started working. A sweep holds
    the range it opened on, and the record it travels through is wider than
    that range, so the trace left the box and kept going. It was always
    possible; holding only made it certain.

    Clipping is also the honest rendering: the axis says what it covers, and
    a line drawn past it asserts a position on an axis that does not extend
    that far.
    """

    def _held_too_narrow(self) -> str:
        # Readings from 0 to 100 against an axis held to 40-60: most of this
        # series belongs outside the box.
        return render_svg(
            [Series(name="wide", unit="degC",
                    points=[(T0 + dt.timedelta(seconds=i), float(i * 5)) for i in range(21)])],
            title="held narrow",
            held_extent={"degC": (40.0, 60.0)},
        )

    def test_the_marks_are_clipped(self):
        svg = self._held_too_narrow()
        assert "<clipPath id=" in svg
        assert re.search(r'clip-path="url\(#axk-plot-[0-9a-f]{8}\)"', svg)

    def test_the_clip_is_the_plot_box_itself(self):
        svg = self._held_too_narrow()
        plot = re.search(r'data-plot="([\d.]+) ([\d.]+) ([\d.]+) ([\d.]+)"', svg)
        assert plot is not None
        left, top, width, height = (float(g) for g in plot.groups())
        path = re.search(r'<clipPath id="axk-plot-[0-9a-f]{8}"><path d="M([\d.]+),([\d.]+) h([\d.]+) v([\d.]+)', svg)
        assert path is not None
        assert abs(float(path.group(1)) - left) < 1
        assert abs(float(path.group(2)) - top) < 1
        assert abs(float(path.group(3)) - width) < 1
        assert abs(float(path.group(4)) - height) < 1

    def test_the_series_are_inside_the_clip_and_the_labels_are_not(self):
        # The labels sit in the right margin, outside the plot. Clipping them
        # would be clipping the thing that names the line.
        svg = self._held_too_narrow()
        opened = re.search(r'clip-path="url\(#axk-plot-[0-9a-f]{8}\)"', svg).start()
        closed = svg.index("</g>", opened)
        drawn = svg[opened:closed]
        assert "<polyline" in drawn, "the marks are not inside the clip"
        after = svg[closed:]
        assert 'font-weight="600"' in after, "the direct labels were clipped away with them"

    def test_the_clip_costs_an_unheld_figure_nothing(self):
        # It is unconditional, so it also has to be harmless where no reading
        # was ever going to leave the box.
        svg = render_svg(
            [Series(name="ordinary", unit="W",
                    points=[(T0 + dt.timedelta(seconds=i), 900.0 + i) for i in range(20)])],
            title="ordinary",
        )
        assert re.search(r'clip-path="url\(#axk-plot-[0-9a-f]{8}\)"', svg)
        assert "<polyline" in svg


class TestTwoFiguresOnOnePageDoNotShareAClip:
    """An SVG id is document-scoped, and two figures live on one page.

    Filling a chart to the screen mounts a SECOND svg beside the page one.
    Both used to emit `clipPath id="axk-plot"`, and `url(#axk-plot)` resolves
    to whichever appears FIRST in the document, so the full-screen figure was
    clipped to the page figure's plot rectangle. A 1920x820 figure clipped to a
    1720x480 one loses everything below 48% of its height and beyond 87% of its
    width.

    Ben reported that as the bottom being cut off, as half the graph being
    chopped, and as the x-axis not reaching the right edge. One cause, three
    descriptions. It survived a long hunt because each svg is correct alone and
    only the pair is wrong, so nothing that inspected one figure could see it.
    """

    def _two_sizes(self):
        small = render_svg([Series("t", _ramp().points, unit="degC")], title="t",
                           geometry=Geometry.for_width(1720, height=480))
        big = render_svg([Series("t", _ramp().points, unit="degC")], title="t",
                         geometry=Geometry.for_width(1920, height=820))
        return small, big

    def test_their_clip_ids_differ(self):
        small, big = self._two_sizes()
        a = re.search(r'<clipPath id="(axk-plot-[0-9a-f]{8})"', small).group(1)
        b = re.search(r'<clipPath id="(axk-plot-[0-9a-f]{8})"', big).group(1)
        assert a != b, f"both figures emitted {a}"

    def test_each_references_its_own(self):
        for svg in self._two_sizes():
            declared = re.search(r'<clipPath id="(axk-plot-[0-9a-f]{8})"', svg).group(1)
            used = re.search(r'clip-path="url\(#(axk-plot-[0-9a-f]{8})\)"', svg).group(1)
            assert declared == used

    def test_the_id_is_stable_for_the_same_figure(self):
        # Derived from geometry, not a counter: the same figure rendered twice
        # is byte-identical, so an export stays reproducible.
        g = Geometry.for_width(1920, height=820)
        once = render_svg([Series("t", _ramp().points, unit="degC")], title="t", geometry=g)
        twice = render_svg([Series("t", _ramp().points, unit="degC")], title="t", geometry=g)
        assert once == twice
