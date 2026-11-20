# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The chart kind is chosen by scoring what is registered.

The first cut was an if-ladder inside one function, so a new kind became
choosable only when somebody remembered to add a branch. Each kind now
declares the DATA it needs — `ChartKind.requires` already said which SPEC
fields it uses, which is the other half of the same question — and the
chooser scores them.

Two consequences worth having: registering a kind makes it choosable, and
the kinds that fit but did not win are named, so a recommendation can be
argued with rather than only accepted.
"""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.scidisplay.chart_choice import offer_for
from axiom.extensions.builtins.scidisplay.chart_spec import (
    ChartKind,
    DataShape,
    RegistrationError,
    lookup_kind,
)

COLUMNS = (("ts", "time", True, "left"),
           ("channel", "channel", True, "left"),
           ("value", "value", True, "right"),
           ("unit", "unit", True, "left"),
           ("source_class", "class", True, "left"))

MODE_COLUMNS = (("ts", "time", True, "left"),
                ("channel", "channel", True, "left"),
                ("mode", "mode", True, "left"))


def readings(n=8, cls="measured", channels=("tc1", "pwr")):
    return [{"ts": f"2026-09-18T09:30:{s:02d}Z", "channel": c, "value": float(s),
             "unit": "degC", "source_class": cls}
            for s in range(n) for c in channels]


def console(n=9):
    return [{"ts": f"2026-09-18T09:30:{s:02d}Z", "channel": "console",
             "mode": "STARTUP" if s < 3 else ("STEADY" if s < 6 else "SCRAM")}
            for s in range(n)]


class TestTheShapeDecides:
    def test_readings_over_time_are_a_timeseries(self):
        assert offer_for(readings(), columns=COLUMNS).kind == "timeseries"

    def test_measured_beside_modelled_is_a_comparison(self):
        """It fits a narrower set of rows, so when it fits it is the more
        informative picture."""
        rows = readings() + readings(cls="predicted")
        assert offer_for(rows, columns=COLUMNS).kind == "comparison"

    def test_labels_over_time_are_a_state(self):
        assert offer_for(console(), columns=MODE_COLUMNS).kind == "state"

    def test_the_runners_up_are_named(self):
        rows = readings() + readings(cls="predicted")
        assert "timeseries" in offer_for(rows, columns=COLUMNS).alternatives

    def test_the_chosen_kind_is_not_also_an_alternative(self):
        offer = offer_for(readings(), columns=COLUMNS)
        assert offer.kind not in offer.alternatives


class TestWhatIsNotAState:
    def test_the_series_column_is_not_a_state(self):
        """`channel` separates the lines. Reading it as a state turned an
        ordinary reading table into a state chart."""
        assert offer_for(readings(), columns=COLUMNS).kind == "timeseries"

    def test_a_unit_is_not_a_state(self):
        """Across a table `unit` holds W and degC, so it looks like a label
        that moves. Within one channel it never moves: it is an attribute
        of the series, not a state of it."""
        rows = [
            {"ts": f"2026-09-18T09:30:{s:02d}Z", "channel": c, "value": float(s), "unit": u}
            for s in range(8) for c, u in (("pwr", "W"), ("tc1", "degC"))
        ]
        offer = offer_for(rows, columns=COLUMNS, series_hint="channel")
        assert offer.kind == "timeseries"

    def test_provenance_is_not_a_state(self):
        """`source_class` splits a series into measured and modelled, which
        the comparison kind consumes. Left as a candidate it also won the
        series column, and a comparison of one thermocouple came out
        labelled "measured" and "predicted" with the channel nowhere on
        the picture."""
        rows = readings() + readings(cls="predicted")
        assert offer_for(rows, columns=COLUMNS).series_column == "channel"


class TestTheCallerNamesItsSeries:
    def test_a_hint_wins_over_the_tie_break(self):
        """Filtered to one channel every candidate holds one distinct
        value, and the arbitrary winner labelled the chart with the feed
        name. Which column names a series is the caller's schema."""
        rows = [{"ts": f"2026-09-18T09:30:0{s}Z", "channel": "tc1",
                 "feed": "site.run", "value": float(s), "unit": "degC"}
                for s in range(5)]
        offer = offer_for(rows, series_hint="channel")
        assert offer.series_column == "channel"
        assert offer.channels == ("tc1",)

    def test_a_hint_naming_nothing_is_ignored(self):
        assert offer_for(readings(), columns=COLUMNS, series_hint="nope").kind


class TestAStipulatedKind:
    def test_it_wins_over_the_inference(self):
        rows = readings() + readings(cls="predicted")
        offer = offer_for(rows, columns=COLUMNS, stipulated="timeseries")
        assert offer.kind == "timeseries"
        assert offer.decided == "stipulated"

    def test_the_inferred_one_says_so(self):
        assert offer_for(readings(), columns=COLUMNS).decided == "inferred"

    def test_a_kind_the_rows_cannot_draw_is_refused(self):
        """Falling back to the inferred kind would hand somebody a picture
        they did not ask for, and no sign that they did not get one."""
        offer = offer_for(readings(), columns=COLUMNS, stipulated="comparison")
        assert not offer.kind
        assert "nothing to compare" in offer.reason

    def test_an_unregistered_kind_names_what_there_is(self):
        offer = offer_for(readings(), columns=COLUMNS, stipulated="piechart")
        assert not offer.kind
        assert "timeseries" in offer.reason


class TestRegisteringMakesAKindChoosable:
    def test_a_new_kind_is_chosen_without_touching_the_chooser(self, tmp_path):
        from axiom.extensions.builtins.scidisplay.chart_spec import (
            register_kind,
            unregister_kind,
        )

        register_kind(ChartKind(
            name="demanding",
            summary="s",
            requires=frozenset({"channels", "window", "title"}),
            needs=DataShape(time_axis=True, numeric_columns=1, series=True,
                            both_provenances=True),
        ))
        try:
            rows = readings() + readings(cls="predicted")
            assert offer_for(rows, columns=COLUMNS).kind == "demanding"
        finally:
            unregister_kind("demanding")

    def test_a_kind_declaring_no_data_needs_is_never_chosen(self):
        """Right default for a kind whose fit only a person can judge: it
        can be asked for and is never guessed at."""
        assert lookup_kind("timeseries").needs is not None

    def test_needs_must_be_a_data_shape(self):
        with pytest.raises(RegistrationError):
            ChartKind(name="bad", summary="s", needs="time")  # type: ignore[arg-type]


class TestOnlyWhatIsRegisteredCanBeOffered:
    def test_a_narrower_registry_falls_back(self):
        rows = readings() + readings(cls="predicted")
        assert offer_for(rows, columns=COLUMNS, registered=("timeseries",)).kind == (
            "timeseries"
        )

    def test_an_empty_registry_offers_nothing_and_says_why(self):
        offer = offer_for(readings(), columns=COLUMNS, registered=())
        assert not offer.kind
        assert "no registered kind" in offer.reason
