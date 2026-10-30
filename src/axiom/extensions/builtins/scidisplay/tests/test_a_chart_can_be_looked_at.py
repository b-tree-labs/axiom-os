# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A chart can be looked at where it was made.

`chart_spec` said, correctly, that "a kind nothing renders would be a
promise" — and nothing in this tree rendered one. The document went to disk
and a web component drew it somewhere else, so every registered kind was a
promise, including the one that shipped.

This is the smallest honest renderer: enough that registering a kind means
something.
"""

from __future__ import annotations

from axiom.extensions.builtins.scidisplay.chart_choice import offer_for
from axiom.extensions.builtins.scidisplay.chart_render import render, sparkline

COLUMNS = (("ts", "time", True, "left"),
           ("channel", "channel", True, "left"),
           ("value", "value", True, "right"),
           ("unit", "unit", True, "left"),
           ("source_class", "class", True, "left"))


def ramp(cls="measured", scale=1.0, n=12):
    return [{"ts": f"2026-09-18T09:30:{s:02d}Z", "channel": "tc1",
             "value": s * scale, "unit": "degC", "source_class": cls}
            for s in range(n)]


class TestTheSparkline:
    def test_a_rise_rises(self):
        line = sparkline([float(i) for i in range(8)], width=8)
        assert line[0] == "▁"
        assert line[-1] == "█"

    def test_a_gap_is_not_a_floor(self):
        """A flat line and missing data look identical otherwise, and they
        are the two readings a person most needs to tell apart."""
        assert "·" in sparkline([1.0, None, 3.0], width=3)

    def test_a_constant_is_not_drawn_on_the_baseline(self):
        """A constant drawn at the floor reads as zero."""
        from axiom.extensions.builtins.scidisplay.chart_render import _BLOCKS

        drawn = set(sparkline([5.0, 5.0, 5.0], width=3))
        assert drawn == {_BLOCKS[len(_BLOCKS) // 2]}
        assert _BLOCKS[0] not in drawn

    def test_it_bucket_to_the_width_asked_for(self):
        assert len(sparkline([float(i) for i in range(500)], width=40)) == 40

    def test_nothing_renders_as_nothing(self):
        assert sparkline([]) == ""


class TestTimeseries:
    def test_each_series_gets_a_row(self):
        rows = ramp() + [{**r, "channel": "pwr", "unit": "W"} for r in ramp()]
        drawn = render(offer_for(rows, columns=COLUMNS, series_hint="channel"), rows)
        assert "tc1" in drawn
        assert "pwr" in drawn

    def test_each_row_carries_its_own_range_and_unit(self):
        """There is no shared axis between kW and degC, and drawing one
        would be a lie about comparability. What a reader compares here is
        shape; the numbers are printed beside it."""
        rows = ramp()
        assert "degC" in render(offer_for(rows, columns=COLUMNS, series_hint="channel"), rows)


class TestComparison:
    def test_both_provenances_are_drawn(self):
        rows = ramp() + ramp(cls="predicted", scale=1.2)
        drawn = render(offer_for(rows, columns=COLUMNS, series_hint="channel"), rows)
        assert "measured" in drawn
        assert "modelled" in drawn

    def test_the_worst_gap_is_stated(self):
        """Two lines that look alike at terminal resolution can still be
        thirty degrees apart, so the number is printed rather than left to
        the eye."""
        rows = ramp() + ramp(cls="predicted", scale=1.2)
        drawn = render(offer_for(rows, columns=COLUMNS, series_hint="channel"), rows)
        assert "worst gap" in drawn
        assert "model high" in drawn

    def test_a_model_with_nothing_to_compare_says_so(self):
        rows = ramp() + [{**r, "channel": "other"} for r in ramp(cls="predicted")]
        drawn = render(offer_for(rows, columns=COLUMNS, series_hint="channel"), rows)
        assert "no measurement to compare" in drawn


class TestState:
    def _console(self, n=9):
        return [{"ts": f"2026-09-18T09:30:{s:02d}Z", "channel": "console",
                 "mode": "STARTUP" if s < 3 else ("STEADY" if s < 6 else "SCRAM")}
                for s in range(n)]

    def test_each_span_is_drawn_once(self):
        rows = self._console()
        drawn = render(offer_for(rows), rows)
        assert drawn.count("STARTUP") == 1
        assert drawn.count("SCRAM") == 1

    def test_how_long_each_lasted_is_the_fact(self):
        """"SCRAM for 2m" is what a reader wants and a line chart cannot
        say."""
        rows = self._console()
        assert "s  from" in render(offer_for(rows), rows)

    def test_a_state_that_returns_gets_two_spans(self):
        rows = [{"ts": f"2026-09-18T09:30:0{s}Z", "channel": "c",
                 "mode": "ON" if s in (0, 1, 4, 5) else "OFF"} for s in range(6)]
        assert render(offer_for(rows), rows).count("ON ") >= 2


class TestNothingToDraw:
    def test_it_renders_the_reason_rather_than_an_empty_picture(self):
        offer = offer_for([])
        assert "no rows" in render(offer, [])
