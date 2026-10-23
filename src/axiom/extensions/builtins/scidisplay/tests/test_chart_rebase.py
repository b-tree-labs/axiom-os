# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Rebasing: every series as its change from a reference."""

from __future__ import annotations

# ---------------------------------------------- a reference that stays put

def _at(minute: int):
    from datetime import UTC, datetime
    return datetime(2026, 9, 10, 7, minute, tzinfo=UTC)


def _series(name, points, unit="degC"):
    from axiom.extensions.builtins.scidisplay.chart_svg import Series
    return Series(name=name, unit=unit, points=[(_at(m), v) for m, v in points])


def test_a_base_near_zero_is_refused_and_says_why():
    """A reactor idling at 0.0004 W before a run to 1 MW gave 3 × 10¹¹ % and
    an axis labelled 300000000000. The figure was about the base's rounding,
    not the readings. Refused, naming both numbers, rather than drawn."""
    from axiom.extensions.builtins.scidisplay.chart_rebase import rebase

    out = rebase([_series("power", [(0, 0.0004), (1, 1e6), (2, 9e5)], unit="W")])
    assert not out.series
    (name, why), = out.refused
    assert name == "power" and "0.0004" in why and "1e+06" in why


def test_a_sweep_frame_measures_from_the_base_it_was_given():
    """The reference travels with the sweep. Each frame took its own first
    reading as the base, so the whole series rescaled per frame and the
    axis flickered between percent and nonsense. A frame handed the bases
    from an earlier frame measures from them instead."""
    from axiom.extensions.builtins.scidisplay.chart_rebase import rebase

    first = rebase([_series("fuel", [(0, 20.0), (1, 400.0), (2, 300.0)])])
    assert first.bases == (("fuel", 20.0),)
    later = rebase(
        [_series("fuel", [(1, 400.0), (2, 300.0)])],
        bases=dict(first.bases),
    )
    # 400 against a base of 20 is +1900 %; against its own first reading it
    # would have been 0 %, which is the flicker.
    assert later.series[0].points[0][1] == 1900.0
    assert later.bases == (("fuel", 20.0),)


def test_a_zero_base_is_still_refused_even_when_given():
    from axiom.extensions.builtins.scidisplay.chart_rebase import rebase

    out = rebase([_series("x", [(0, 5.0), (1, 6.0)])], bases={"x": 0.0})
    assert out.refused and "zero" in out.refused[0][1]


# ---------------------------------------- the time axis is the window

def test_the_time_axis_is_the_window_not_the_data():
    """A reading included from beyond an edge — so a trace slopes off the plot
    rather than stopping at its last reading — must not stretch the axis to
    reach it. It did: a window of 08:00–12:00 drew its axis from the 07:59
    neighbour, and a sweep frame drawn a tenth past its edge stretched the
    axis a tenth. With the window named, the axis is the window and the
    outside readings are drawn and clipped."""
    import re
    from datetime import UTC, datetime

    from axiom.extensions.builtins.scidisplay.chart_svg import Series, render_svg

    def at(h, m=0):

        return datetime(2026, 9, 10, h, m, tzinfo=UTC)
    s = Series(name="power", unit="W", points=[(at(7, 59), 1.0), (at(9), 2.0), (at(11), 3.0), (at(12, 24), 4.0)])
    svg = render_svg([s], window=(at(8), at(12)))
    edges = dict(re.findall(r'data-edge="(start|end)" text-anchor="\w+">([^<]*)', svg))
    assert edges == {"start": "08:00", "end": "12:00"}, edges
    # Without a window the old behaviour stands, for a caller drawing the data as is.
    bare = render_svg([s])
    assert "07:59" in bare and "12:24" in bare


# ---------------------------------------- the direct labels sit at the plot's edge

def _leader_and_label(svg: str) -> tuple[tuple[float, float, float, float], float, str]:
    """The one direct label's leader line (x1, y1, x2, y2), its text x, and
    the reading it prints."""
    import re

    leader = re.search(
        r'<line x1="([\d.]+)" y1="([\d.]+)" x2="([\d.]+)" y2="([\d.]+)" '
        r'stroke="#[0-9a-fA-F]+" stroke-width="[\d.]+" opacity="0.5"/>', svg
    )
    assert leader, "no direct-label leader drawn"
    label = re.search(r'<text x="([\d.]+)" y="[\d.]+" font-size="[\d.]+" fill="#[0-9a-fA-F]+" '
                      r'font-family="[^"]*" font-weight="600">power<tspan[^>]*>([^<]*)</tspan>', svg)
    assert label, "no direct label drawn"
    return tuple(float(v) for v in leader.groups()), float(label.group(1)), label.group(2)


def _plot_right(svg: str) -> float:
    import re

    left, _top, width, _height = (float(v) for v in re.search(r'data-plot="([^"]+)"', svg).group(1).split())
    return left + width


def test_a_label_anchors_where_the_trace_leaves_the_plot_not_at_the_last_reading_drawn():
    """A reading drawn from beyond the window's end — the bracketing reading
    a close zoom includes so the trace slopes off the plot, or the tenth a
    sweep frame is drawn past its edge — must not carry the label with it.

    It did. The label anchored at the last reading DRAWN, so at a close zoom
    and on every play frame the leader ran off through the right margin and
    the label landed past the figure's edge: a column of names gone from
    where the reader looks, and the margin they left read as a gap. Ben:
    "zoomed in ... hit run and it ended up with this gap on the right side.
    It seems to happen whether I zoom or not."

    The leader starts where the drawn line crosses the plot's right edge,
    and the label prints the last reading INSIDE the window — never a value
    interpolated at the edge, which would be a number nobody measured."""
    from datetime import UTC, datetime

    from axiom.extensions.builtins.scidisplay.chart_svg import Series, render_svg

    def at(h, m=0):

        return datetime(2026, 9, 10, h, m, tzinfo=UTC)
    beyond = Series(name="power", unit="W", points=[(at(9), 1.0), (at(11), 3.0), (at(13), 5.0)])
    svg = render_svg([beyond], window=(at(8), at(12)))
    (x1, y1, x2, _y2), label_x, reading = _leader_and_label(svg)
    right = _plot_right(svg)
    assert abs(x1 - right) < 0.11, (x1, right)
    assert x2 > right and label_x > right, (x2, label_x, right)
    assert reading == "3 W", reading
    # The crossing is ON the drawn line: the same height as a reading of 4.0
    # would be at 12:00, which is what the line between 3.0 at 11:00 and 5.0
    # at 13:00 passes through there.
    measured = Series(name="power", unit="W", points=[(at(9), 1.0), (at(11), 3.0), (at(12), 4.0), (at(13), 5.0)])
    (_x1, y1_measured, *_), *_ = _leader_and_label(render_svg([measured], window=(at(8), at(12))))
    assert abs(y1 - y1_measured) < 0.11, (y1, y1_measured)


def test_a_trace_that_stops_short_of_the_edge_is_still_labelled_at_its_end():
    """A series whose record ends inside the window keeps its label at its
    last reading, so the label still points at where the line stops."""
    from datetime import UTC, datetime

    from axiom.extensions.builtins.scidisplay.chart_svg import Series, render_svg

    def at(h, m=0):

        return datetime(2026, 9, 10, h, m, tzinfo=UTC)
    short = Series(name="power", unit="W", points=[(at(9), 1.0), (at(10), 2.0)])
    svg = render_svg([short], window=(at(8), at(12)))
    (x1, *_), _label_x, reading = _leader_and_label(svg)
    right = _plot_right(svg)
    left = right - float(svg.split('data-plot="')[1].split()[2])
    assert abs(x1 - (left + (right - left) / 2)) < 0.11, (x1, left, right)
    assert reading == "2 W", reading


def test_a_break_before_the_edge_does_not_draw_a_leader_across_it():
    """When the run breaks (an unplottable reading) between the last reading
    inside the window and the first beyond it, no line crosses the edge, so
    the leader starts at the last reading inside — not at a crossing the
    figure never drew."""
    from datetime import UTC, datetime

    from axiom.extensions.builtins.scidisplay.chart_svg import Series, render_svg

    def at(h, m=0):

        return datetime(2026, 9, 10, h, m, tzinfo=UTC)
    broken = Series(name="power", unit="W", points=[(at(9), 1.0), (at(10), 2.0), (at(11), None), (at(13), 5.0)])
    svg = render_svg([broken], window=(at(8), at(12)))
    (x1, *_), _label_x, reading = _leader_and_label(svg)
    right = _plot_right(svg)
    left = right - float(svg.split('data-plot="')[1].split()[2])
    assert abs(x1 - (left + (right - left) / 2)) < 0.11, (x1, left, right)
    assert reading == "2 W", reading


def test_two_labels_are_a_full_line_of_white_apart():
    """Crowded labels are nudged apart by two lines of leading — one line of
    text and one of air. At 1.45 lines (worked out when a label was a name
    ABOVE its value, and left behind when the two moved onto one line) two
    labels in similar colours could not be told apart. Ben: "the labels and
    the graphs are too close together ... you can't see which line goes to
    which label, especially if they're two similar colors."
    """
    import re
    from datetime import UTC, datetime

    from axiom.extensions.builtins.scidisplay.chart_svg import Series, render_svg

    def at(h):

        return datetime(2026, 9, 10, h, tzinfo=UTC)
    # Two series a hair apart in value: their labels must be nudged.
    a = Series(name="RTD1", unit="degC", points=[(at(9), 21.13), (at(11), 21.13)])
    b = Series(name="RTD2", unit="degC", points=[(at(9), 21.14), (at(11), 21.14)])
    svg = render_svg([a, b], window=(at(9), at(11)))
    found = re.findall(
        r'<text x="[\d.]+" y="([\d.]+)" font-size="([\d.]+)"[^>]*font-weight="600">RTD\d', svg
    )
    assert len(found) == 2, found
    apart = abs(float(found[0][0]) - float(found[1][0]))
    size = float(found[0][1])
    assert apart >= size * 1.9, (apart, size)


def test_pressing_play_does_not_move_a_single_label():
    """A sweep pins the axis so the picture stays still while the data moves
    through it. It used to pin the LABELS too, in a stack of its own, so the
    figure rearranged itself the instant somebody pressed play — first
    huddling them, then spreading them. Ben: "the distance between the labels
    should not change just because you hit play."

    The same readings laid out with and without a held axis are the same
    figure, label for label."""
    import re
    from datetime import UTC, datetime

    from axiom.extensions.builtins.scidisplay.chart_svg import Series, render_svg

    def at(h):

        return datetime(2026, 9, 10, h, tzinfo=UTC)
    series = [
        Series(name="RTD1", unit="degC", points=[(at(9), 5.0), (at(11), 5.2)]),
        Series(name="RTD2", unit="degC", points=[(at(9), 95.0), (at(11), 94.0)]),
        # Two a hair apart, so the nudge is exercised in both layouts.
        Series(name="RTD3", unit="degC", points=[(at(9), 50.0), (at(11), 50.0)]),
        Series(name="RTD4", unit="degC", points=[(at(9), 50.1), (at(11), 50.1)]),
    ]
    def labels(svg: str) -> list[tuple[str, float]]:
        found = re.findall(
            r'<text x="[\d.]+" y="([\d.]+)" font-size="[\d.]+"[^>]*font-weight="600">(RTD\d)', svg
        )
        return sorted((name, round(float(y), 1)) for y, name in found)

    from axiom.extensions.builtins.scidisplay.chart_svg import pins_in

    held = {"degC": (0.0, 100.0)}
    before = render_svg(series, window=(at(9), at(11)), held_extent=held)
    first = render_svg(series, window=(at(9), at(11)), held_extent=held,
                       held_labels=pins_in(before))
    assert len(labels(before)) == 4, labels(before)
    assert labels(before) == labels(first), (labels(before), labels(first))
    # And the frame after that, with the data moved on, is the same again.
    moved = [
        Series(name=s.name, unit=s.unit, points=[(t, v + 3.0) for t, v in s.points])
        for s in series
    ]
    later = render_svg(moved, window=(at(9), at(11)), held_extent=held,
                       held_labels=pins_in(before))
    assert labels(before) == labels(later), (labels(before), labels(later))
