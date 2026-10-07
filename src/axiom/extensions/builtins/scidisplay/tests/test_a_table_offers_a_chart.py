# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A table says whether its rows are worth drawing, and as what.

The table is where somebody is looking when they decide to plot. It already
holds everything needed to answer — a time axis, a numeric column, more
than one instant, something separating one series from another — and said
nothing, leaving them to work it out and type the command from memory.

It picks from what is REGISTERED, not from a catalogue. `chart_spec` ships
one kind on purpose ("a kind nothing renders would be a promise"), so this
returns `timeseries` or a reason, and never names a kind nothing can draw.
"""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.scidisplay.chart_choice import offer_for
from axiom.extensions.builtins.scidisplay.table_spec import tabulate

COLUMNS = (("ts", "time", True, "left"),
           ("channel", "channel", True, "left"),
           ("value", "value", True, "right"),
           ("unit", "unit", True, "left"),
           ("source_class", "class", True, "left"))


def _rows(n=6, channels=("fuel_temp_1", "power"), cls="measured"):
    return [
        {"ts": f"2026-09-18T09:30:{s:02d}Z", "channel": c, "value": float(s),
         "unit": "degC", "source_class": cls}
        for s in range(n) for c in channels
    ]


class TestWhenItOffers:
    def test_readings_over_time_are_a_timeseries(self):
        assert offer_for(_rows(), columns=COLUMNS).kind == "timeseries"

    def test_it_names_the_axis_it_found(self):
        assert offer_for(_rows(), columns=COLUMNS).time_column == "ts"

    def test_it_names_the_column_that_separates_the_lines(self):
        """A chart wants the column that DISTINGUISHES — `unit` holds one
        value here and would draw everything as one line, hiding the very
        comparison somebody opened the chart for."""
        offer = offer_for(_rows(), columns=COLUMNS)
        assert offer.series_column == "channel"
        assert offer.channels == ("fuel_temp_1", "power")

    def test_the_axis_is_found_by_its_VALUES_not_its_name(self):
        """A column called `ts` holding free text is not a time axis, and
        one called `t0` holding timestamps is. Names are a convention and
        conventions differ per site."""
        rows = [{"t0": f"2026-09-18T09:30:0{s}Z", "ts": "not a time", "v": float(s)}
                for s in range(5)]
        offer = offer_for(rows, columns=(("t0", "t0", True, "left"),
                                         ("ts", "ts", True, "left"),
                                         ("v", "v", True, "right")))
        assert offer.time_column == "t0"


class TestWhenItRefuses:
    @pytest.mark.parametrize(
        "rows, expected",
        [
            ([], "no rows"),
            ([{"ts": "2026-09-18T09:30:00Z", "channel": "a", "value": 1.0,
               "unit": "x", "source_class": "measured"}], "same instant"),
            ([{"channel": "a", "value": float(i)} for i in range(5)], "no time axis"),
            ([{"ts": f"2026-09-18T09:30:0{i}Z", "channel": "a"} for i in range(5)],
             "no numeric column"),
        ],
    )
    def test_it_says_why(self, rows, expected):
        """A refusal that does not say why sends somebody to reshape data
        that was already fine."""
        offer = offer_for(rows)
        assert not offer.kind
        assert expected in offer.reason

    def test_an_unregistered_kind_is_never_named(self):
        """The whole discipline: naming a kind nothing renders is a
        promise, and this is the one place that would be tempted to.

        The wording moved when the chooser began scoring every registered
        kind rather than asking after `timeseries` by name."""
        offer = offer_for(_rows(), columns=COLUMNS, registered=())
        assert offer.kind == ""
        assert "no registered kind" in offer.reason


class TestTheForecastCase:
    def test_measured_beside_modelled_is_recognised(self):
        rows = _rows() + _rows(cls="predicted")
        assert offer_for(rows, columns=COLUMNS).compares_model_to_measurement

    def test_the_reason_says_what_drawing_them_together_is_for(self):
        rows = _rows() + _rows(cls="predicted")
        assert "drifting" in offer_for(rows, columns=COLUMNS).reason

    def test_one_class_alone_is_not_a_comparison(self):
        assert not offer_for(_rows(), columns=COLUMNS).compares_model_to_measurement


class TestTheTableCarriesIt:
    def test_the_view_holds_the_offer(self):
        assert tabulate(_rows(), columns=COLUMNS, title="t")["chart"]["kind"] == "timeseries"

    def test_it_is_decided_over_the_MATCHED_rows_not_the_page(self):
        """A chart of page one of ten is a chart of an arbitrary
        twenty-five readings."""
        view = tabulate(_rows(n=40), columns=COLUMNS, title="t", page_size=2)
        assert view["chart"]["instants"] == 40

    def test_it_respects_the_filter(self):
        view = tabulate(_rows(), columns=COLUMNS, title="t",
                        equals=(("channel", "power"),))
        assert view["chart"]["channels"] == ["power"]

    def test_a_table_that_cannot_be_charted_says_so_rather_than_nothing(self):
        view = tabulate([{"channel": "a", "value": 1.0}], title="t",
                        columns=(("channel", "c", True, "left"),
                                 ("value", "v", True, "right")))
        assert view["chart"]["kind"] == ""
        assert view["chart"]["reason"]

    def test_an_offer_never_breaks_a_table(self, monkeypatch):
        from axiom.extensions.builtins.scidisplay import table_spec

        monkeypatch.setattr(
            table_spec, "_chart_offer",
            lambda *a, **k: {"kind": "", "reason": "could not decide: boom"},
        )
        assert tabulate(_rows(), columns=COLUMNS, title="t")["rows"]


class TestItDoesNotMisdescribeTheData:
    """A recommendation that misdescribes the data is worse than no
    recommendation: the reader trusts it and stops looking."""

    def _at(self, stamps, value=None):
        return [
            {"ts": t, "channel": "tc1",
             "value": float(i) if value is None else value, "unit": "degC"}
            for i, t in enumerate(stamps)
        ]

    def test_a_flat_series_is_not_called_trending(self):
        rows = self._at([f"2026-09-01T00:00:{s:02d}Z" for s in range(30)], value=2.0)
        offer = offer_for(rows, series_hint="channel")
        assert offer.kind == "timeseries", "flat is still worth drawing"
        # "not trending" contains "trend", so the claim is what is
        # asserted rather than the substring.
        assert "flat, not trending" in offer.reason

    def test_a_flat_series_says_what_the_value_is(self):
        """"Did anything happen" is a real question and "no, it sat at 2"
        is the answer."""
        rows = self._at([f"2026-09-01T00:00:{s:02d}Z" for s in range(30)], value=2.0)
        assert "2" in offer_for(rows, series_hint="channel").reason

    def test_two_distant_readings_are_not_called_a_line(self):
        """A line between them asserts a continuity nobody measured — the
        objection this codebase already makes to interpolating a step-held
        channel."""
        rows = self._at(["2026-03-01T00:00:00Z", "2026-09-01T00:00:00Z"])
        assert "invent" in offer_for(rows, series_hint="channel").reason

    def test_readings_a_day_apart_are_flagged_too(self):
        rows = self._at([f"2026-09-0{d}T00:00:00Z" for d in (1, 2, 3, 4)])
        assert "invent" in offer_for(rows, series_hint="channel").reason

    def test_a_dense_series_is_not_flagged(self):
        rows = self._at([f"2026-09-01T00:00:{s:02d}Z" for s in range(40)])
        assert "trend" in offer_for(rows, series_hint="channel").reason

    def test_a_week_of_daily_readings_is_a_series(self):
        """Five or more readings is a cadence, however wide the gaps. Only
        a handful flung across a window are islands."""
        rows = self._at([f"2026-09-0{d}T00:00:00Z" for d in range(1, 8)])
        assert "trend" in offer_for(rows, series_hint="channel").reason

    def test_three_readings_minutes_apart_are_a_series(self):
        rows = self._at([f"2026-09-01T00:0{m}:00Z" for m in (0, 5, 10)])
        assert "invent" not in offer_for(rows, series_hint="channel").reason
