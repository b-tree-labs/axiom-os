# Copyright (c) 2026 B-Tree Ventures, LLC
# SPDX-License-Identifier: Apache-2.0
"""A second axis, and a logarithmic one.

Both exist because a real surface needs them. The legacy site's power panel is
logarithmic, because reactor power spans decades and a linear axis renders it as
a flat line with one spike. The analytics dashboard's integrated-energy chart
carries a second axis.

The renderer previously refused two units outright, and its reasoning was right:
two units against ONE labelled axis is the most confidently wrong thing it could
draw, because a temperature overlaid on a power axis is read against watts. That
objection is answered by drawing and labelling BOTH sides, not by ignoring it —
so a second axis is opt-in, a third is still refused, and the default is
unchanged.

The logarithm has its own trap, and it is this programme's favourite shape: the
logarithm of zero is not a small number, it is no number. Reactor power IS zero
at shutdown. Pushing those readings onto the floor would draw a measurement at a
value nobody recorded, so they become gaps — and the figure says how many,
because a log axis quietly discarding readings just looks like a shorter line.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

from axiom.extensions.builtins.scidisplay.chart_svg import Series, render_svg

T0 = datetime(2026, 9, 25, tzinfo=UTC)


def _pts(values):
    return [(T0 + timedelta(minutes=i), v) for i, v in enumerate(values)]


def _notes(svg: str) -> list[str]:
    return re.findall(r'font-style="italic">([^<]+)<', svg)


def _axis_units(svg: str) -> list[str]:
    return sorted(set(re.findall(r'letter-spacing="0.05em">([^<]+)<', svg)))


class TestASecondUnitTakesTheSecondAxisByItself:
    """It used to require the caller to name the unit, and a caller that does
    not know which unit will LEAD cannot name the other one. "Compare this
    temperature against that flow rate" then came back as one line and a note.

    Two units against two LABELLED axes is a figure that reads correctly. It
    is only two units against ONE labelled axis that is confidently wrong, and
    that is still impossible.
    """

    def _two(self):
        return [Series(name="P", points=_pts([100.0, 200.0]), unit="W"),
                Series(name="T", points=_pts([24.0, 25.0]), unit="degC")]

    def test_both_are_drawn(self):
        assert render_svg(self._two(), title="t").count("<polyline") == 2

    def test_both_axes_are_labelled(self):
        assert set(_axis_units(render_svg(self._two(), title="t"))) == {"W", "degC"}

    def test_nothing_is_left_out_to_report(self):
        assert not [n for n in _notes(render_svg(self._two(), title="t")) if "not drawn" in n]

    def test_naming_the_secondary_by_hand_still_works(self):
        svg = render_svg(self._two(), title="t", secondary_unit="degC")
        assert set(_axis_units(svg)) == {"W", "degC"}

    def test_a_third_is_still_refused_and_named(self):
        """Nobody holds three scales."""
        svg = render_svg(
            [*self._two(), Series(name="B", points=_pts([1.0, 2.0]), unit="bar")],
            title="t")
        assert svg.count("<polyline") == 2
        assert any("not drawn" in n and "B" in n for n in _notes(svg))


class TestASecondAxisIsDrawnAndLabelled:
    def _dual(self):
        return render_svg(
            [Series(name="Power", points=_pts([100.0, 200.0, 400.0]), unit="W"),
             Series(name="FuelTemp", points=_pts([24.0, 25.0, 26.5]), unit="degC")],
            title="dual", secondary_unit="degC")

    def test_both_units_are_drawn(self):
        assert self._dual().count("<polyline") == 2

    def test_both_axes_carry_their_unit(self):
        """The whole justification. Two units against one labelled axis is the
        failure; two units against two labelled axes is a figure."""
        assert _axis_units(self._dual()) == ["W", "degC"]

    def test_the_second_axis_has_its_own_numbers(self):
        assert 'text-anchor="start"' in self._dual()

    def test_a_third_unit_is_still_refused(self):
        svg = render_svg(
            [Series(name="a", points=_pts([1.0, 2.0]), unit="W"),
             Series(name="b", points=_pts([3.0, 4.0]), unit="degC"),
             Series(name="c", points=_pts([5.0, 6.0]), unit="bar")],
            title="t", secondary_unit="degC")
        assert svg.count("<polyline") == 2
        assert any("not drawn" in n and "bar" in n for n in _notes(svg))

    def test_naming_a_unit_nothing_carries_changes_nothing(self):
        svg = render_svg([Series(name="a", points=_pts([1.0, 2.0]), unit="W")],
                         title="t", secondary_unit="furlongs")
        assert _axis_units(svg) == ["W"]


class TestALogarithmicAxis:
    def test_it_ticks_in_decades(self):
        svg = render_svg([Series(name="P", points=_pts([1.0, 100.0, 10000.0]), unit="W")],
                         title="t", log_units=("W",))
        # A linear tick sequence over four decades bunches at the top; decades
        # do not. Several distinct powers of ten must appear.
        assert svg.count("<line") > 3

    def test_a_decade_span_is_drawn_at_all(self):
        svg = render_svg(
            [Series(name="P", points=_pts([1e-4, 1e-2, 1.0, 1e2, 1e4]), unit="W")],
            title="t", log_units=("W",))
        assert "<polyline" in svg


class TestZeroOnALogAxisIsAGapNotTheFloor:
    """The trap. Reactor power is zero at shutdown."""

    def test_the_line_breaks_at_a_zero(self):
        svg = render_svg(
            [Series(name="P", points=_pts([1.0, 10.0, 0.0, 100.0, 1000.0]), unit="W")],
            title="t", log_units=("W",))
        assert svg.count("<polyline") == 2

    def test_a_negative_is_a_gap_too(self):
        svg = render_svg(
            [Series(name="P", points=_pts([1.0, 10.0, -5.0, 100.0, 1000.0]), unit="W")],
            title="t", log_units=("W",))
        assert svg.count("<polyline") == 2

    def test_the_figure_SAYS_how_many_it_could_not_place(self):
        """Because a log axis silently discarding readings just looks like a
        shorter line, which is the quietest way for a figure to be wrong."""
        svg = render_svg(
            [Series(name="P", points=_pts([1.0, 0.0, 0.0, 100.0]), unit="W")],
            title="t", log_units=("W",))
        assert any("2 reading" in n and "logarithmic" in n for n in _notes(svg))

    def test_a_linear_axis_still_plots_zero_normally(self):
        """Zero is a perfectly good reading. It is only the logarithm that
        cannot place it, so the same series on a linear axis is unbroken."""
        svg = render_svg(
            [Series(name="P", points=_pts([1.0, 10.0, 0.0, 100.0]), unit="W")],
            title="t")
        assert svg.count("<polyline") == 1
        assert not any("logarithmic" in n for n in _notes(svg))

    def test_every_mark_honours_it(self):
        for mark in ("bar", "scatter"):
            svg = render_svg(
                [Series(name="P", points=_pts([1.0, 0.0, 100.0]), unit="W", mark=mark)],
                title="t", log_units=("W",))
            drawn = svg.count("<rect") - 1 if mark == "bar" else svg.count("<circle")
            assert drawn == 2, f"{mark} drew the zero"


class TestTheSecondAxisDoesNotLandOnTheLabels:
    """The right margin already holds the direct label at the end of each
    line. An axis drawn into it without reserving room puts its numbers on
    top of the series names, and the figure still renders."""

    def _positions(self, svg: str) -> tuple[float, float]:
        # An axis is a VERTICAL line, which is a fact about the figure. It
        # used to be found by `stroke="#1`, a fact about how the ink was
        # spelled — and the ink is now `var(--axk-plot-ink,#1a1a1a)` so the
        # figure could follow a dark page. Nothing about the geometry moved;
        # the test was reading the paint to find the shape.
        verticals = [
            (float(x1), float(x2))
            for x1, _y1, x2, _y2 in re.findall(
                r'<line x1="([\d.]+)" y1="([\d.]+)" x2="([\d.]+)" y2="([\d.]+)"', svg
            )
        ]
        axis = max(x1 for x1, x2 in verticals if abs(x1 - x2) < 0.5)
        # The direct labels are the bold ones: the series name and its last
        # reading, set on one line. "Any <text> with a <tspan>" used to
        # identify them and no longer does — the time axis now hangs a date
        # under its end ticks, and the leftmost of those sits at the plot's
        # left edge, which is not a name and not near this axis.
        names = min(
            float(m)
            for m in re.findall(r'<text x="([\d.]+)"[^>]*>[^<]*<tspan[^>]*dx=', svg)
        )
        return axis, names

    def test_the_names_sit_clear_of_the_axis(self):
        svg = render_svg(
            [Series(name="Power", points=_pts([1000.0, 200000.0]), unit="W"),
             Series(name="FuelTemp", points=_pts([24.0, 480.0]), unit="degC")],
            title="t", secondary_unit="degC")
        axis_x, first_name_x = self._positions(svg)
        assert first_name_x > axis_x + 20, (axis_x, first_name_x)

    def test_asking_for_a_second_axis_does_not_narrow_the_image(self):
        """The gutter comes out of the plot, not the canvas, so a figure sized
        for a journal column stays that WIDE.

        The height is a different matter: it grows to hold whatever the figure
        had to say about itself, because a footer running off the bottom of
        the canvas is worse than a canvas that is taller than it was."""
        one = render_svg([Series(name="P", points=_pts([1.0, 2.0]), unit="W")], title="t")
        two = render_svg(
            [Series(name="P", points=_pts([1.0, 2.0]), unit="W"),
             Series(name="T", points=_pts([24.0, 25.0]), unit="degC")],
            title="t", secondary_unit="degC")

        def size(svg: str) -> tuple[int, int]:
            w, h = re.search(r'width="(\d+)" height="(\d+)"', svg).groups()
            return int(w), int(h)

        assert size(one)[0] == size(two)[0]

    def test_the_canvas_grows_to_hold_what_the_figure_says_about_itself(self):
        """The bottom margin is sized before the notes exist — they depend on
        what the figure turned out to be able to draw. A reader saw the axis
        labels, the notes and the provenance line running through each other.
        """
        quiet = render_svg([Series(name="P", points=_pts([1.0, 2.0]), unit="W")],
                           title="t", provenance="a b c")
        talkative = render_svg(
            [Series(name="P", points=_pts([1.0, 2.0]), unit="W"),
             Series(name="T", points=_pts([24.0, 25.0]), unit="degC"),
             Series(name="B", points=_pts([3.0, 4.0]), unit="bar")],
            title="t", provenance="a b c")

        def height(svg: str) -> int:
            return int(re.search(r'height="(\d+)"', svg).group(1))

        assert height(talkative) >= height(quiet)
        assert f'viewBox="0 0 920 {height(talkative)}"' in talkative


class TestAnAxisThatCanBeRead:
    """Both of these were found by rendering the real UT startup rather than a
    fixture: ten decades of reactor power against fuel temperature."""

    def test_a_log_axis_keeps_its_unit_bare(self):
        """A prefix exists so numbers can be said out loud, and over ten
        decades it only shifts every exponent: an axis in MW labels its bottom
        `1e-10` for a reading of a tenth of a milliwatt."""
        svg = render_svg(
            [Series(name="Power", points=_pts([1e-4, 1.0, 1.1e6]), unit="W")],
            title="t", log_units=("W",))
        assert _axis_units(svg) == ["W"]

    def test_a_linear_axis_still_takes_one(self):
        svg = render_svg(
            [Series(name="Power", points=_pts([1.0, 1.1e6]), unit="W")], title="t")
        assert _axis_units(svg) == ["MW"]

    def test_the_reading_on_the_label_uses_the_axis_scale(self):
        """An axis in W beside a label in MW is the small inconsistency that
        makes a reader stop trusting the figure."""
        svg = render_svg(
            [Series(name="Power", points=_pts([1e-4, 1.0, 1.1e6]), unit="W")],
            title="t", log_units=("W",))
        assert "MW" not in svg

    def test_two_ticks_is_not_a_scale(self):
        """0..363 degC picked a step of 200, labelling the axis 0 and 200 while
        the data topped out at 363 — the reader had to extrapolate past the
        last tick to place the line."""
        svg = render_svg(
            [Series(name="Power", points=_pts([1.0, 2.0]), unit="W"),
             Series(name="FuelTemp1", points=_pts([0.0, 363.0]), unit="degC")],
            title="t", secondary_unit="degC")
        right = re.findall(r'text-anchor="start">([\d.-]+)<', svg)
        assert len(right) >= 3, right


class TestTheTimeAxisSaysWhichDay:
    """A two-day window ticked every twelve hours labelled itself
    `00:00 · 12:00 · 00:00 · 12:00` — the same labels twice over, with nothing
    saying which day either midnight belonged to. Zooming redrew it faithfully
    and looked like nothing had happened, because the labels that came back
    were the labels that left.
    """

    def _labels(self, span):
        n = 120
        points = [(T0 + span * i / n, 1.0 + (i % 5)) for i in range(n + 1)]
        svg = render_svg([Series(name="v", points=points, unit="W")], title="t")
        return re.findall(
            r'text-anchor="(?:middle|start|end)">([^<]*?)'
            r'(?:<tspan[^>]*>([^<]*)</tspan>)?</text>',
            svg,
        )

    def _times(self, span):
        return [(a, b) for a, b in self._labels(span) if re.match(r"^\d\d:", a)]

    def test_a_window_inside_one_day_is_dated_once_at_its_opening(self):
        """It used to be dated NOWHERE, on the reasoning that the provenance
        line under the plot already carries the timestamps.

        It does, in 8-point grey, as `2026-07-24T18:32:46.373000+00:00`. A
        reader looking at `14:06 · 15:00 · 16:00` and wanting to know which
        afternoon this was had to go and parse that. One date, at the end of
        the axis where the window opens, answers it at a glance — and saying
        it again under every tick is the repetition this class exists to
        prevent.
        """
        dated = [b for _a, b in self._times(timedelta(hours=3)) if b]
        assert len(dated) == 1, f"expected the opening date and only that: {dated}"
        assert dated[0] == self._times(timedelta(hours=3))[0][1]

    def test_both_ends_of_a_window_that_crosses_midnight_are_dated(self):
        """"Which day did this start, which day did it end" is one question,
        and answering half of it leaves the reader doing the other half."""
        labels = self._times(timedelta(hours=26))
        assert labels[0][1] and labels[-1][1], (
            f"an undated end: opens {labels[0]}, closes {labels[-1]}"
        )
        assert labels[0][1] != labels[-1][1]

    def test_a_window_that_crosses_midnight_dates_the_crossing(self):
        dated = [b for _a, b in self._times(timedelta(days=1)) if b]
        assert dated, "a window spanning two days named neither"

    def test_and_the_first_tick_too_so_the_axis_starts_somewhere_known(self):
        first = self._times(timedelta(days=2))[0]
        assert first[1], f"the axis opens at {first[0]} on an unnamed day"

    def test_every_midnight_is_told_apart_from_the_others(self):
        """The exact failure: a two-day window has more than one midnight, and
        they all carried the same label."""
        dates = [b for a, b in self._times(timedelta(days=2)) if a == "00:00"]
        assert len(dates) >= 2, "a two-day window drew fewer midnights than it has"
        assert len(set(dates)) == len(dates), f"two midnights share a label: {dates}"

    def test_but_a_date_is_not_repeated_on_every_tick(self):
        """Which is the noise the resolution rule already avoids."""
        labels = self._times(timedelta(days=2))
        assert sum(1 for _a, b in labels if b) < len(labels)

    def test_an_axis_is_never_left_with_one_label(self):
        """"Nearest to the wanted count" picked a SEVEN-DAY step for a nine-day
        window and drew one tick. One label does not say where anything is."""
        for days in (2, 5, 9, 13, 40):
            drawn = self._labels(timedelta(days=days))
            ticks = [a for a, _b in drawn if re.match(r"^\d", a)]
            assert len(ticks) >= 3, f"{days} days drew {ticks}"
