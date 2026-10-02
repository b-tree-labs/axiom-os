# Copyright (c) 2026 B-Tree Ventures, LLC
# SPDX-License-Identifier: Apache-2.0
"""Rows into series, so every surface draws the same figure from the same rows.

The step is small and entirely mechanical, which is exactly why it was written
out by hand in the one place that drew charts. A second surface would have
written it again, and the two would have disagreed within a month about which
rows are modelled, what happens to a row with no timestamp, and where a
channel's unit comes from.

These are the answers, and they are answers about drawing rather than about
acquisition or about the web, which is why they live in the platform.
"""

from __future__ import annotations

from datetime import UTC, datetime

from axiom.extensions.builtins.scidisplay.chart_data import series_from_rows


def _row(**over):
    row = {"ts": "2026-09-22T14:00:00Z", "channel": "Power", "value": 1.0, "unit": "W"}
    row.update(over)
    return row


class TestARowThatIsNotAReading:
    def test_no_readable_instant_is_dropped_rather_than_placed_at_the_epoch(self):
        m = series_from_rows([_row(), _row(ts="not a time")])
        assert m.unusable == 1
        assert len(m.series[0].points) == 1

    def test_a_missing_value_is_dropped_rather_than_drawn_at_zero(self):
        """Zero is a reading. A row that has none is not one."""
        m = series_from_rows([_row(), _row(ts="2026-09-22T14:01:00Z", value=None)])
        assert m.unusable == 1

    def test_a_boolean_is_not_a_measurement(self):
        """`True` is an int in Python, and would have plotted as 1."""
        m = series_from_rows([_row(value=True)])
        assert m.unusable == 1 and m.series == []

    def test_the_count_is_returned_so_a_caller_can_say_how_many(self):
        """Quietly showing fewer readings than were asked for is the failure
        this exists to prevent."""
        m = series_from_rows([_row(ts="x"), _row(ts="y"), _row(value="")])
        assert m.unusable == 3

    def test_zero_really_does_survive(self):
        m = series_from_rows([_row(value=0.0)])
        assert m.unusable == 0 and m.series[0].points[0][1] == 0.0


class TestWhatCountsAsModelled:
    def test_a_declared_provenance_makes_its_own_series(self):
        for word in ("predicted", "estimated", "simulated", "MODELLED"):
            m = series_from_rows([_row(), _row(source_class=word)])
            assert [s.modelled for s in m.series] == [False, True], word

    def test_it_is_paired_with_what_it_models(self):
        """Which is what lets the renderer draw it in that channel's colour,
        dashed, with the divergence shaded."""
        m = series_from_rows([_row(), _row(source_class="predicted")])
        assert m.series[1].against == "Power"

    def test_a_model_with_nothing_to_model_says_so_rather_than_guessing(self):
        m = series_from_rows([_row(channel="ROM", source_class="predicted")])
        assert m.series[0].modelled is True and m.series[0].against == ""

    def test_a_row_that_does_not_say_is_a_measurement(self):
        """Absent provenance is not absent data."""
        assert series_from_rows([_row()]).series[0].modelled is False

    def test_an_unrecognised_provenance_is_a_measurement(self):
        assert series_from_rows([_row(source_class="measured")]).series[0].modelled is False


class TestWhereAUnitComesFrom:
    def test_the_first_one_its_rows_declare(self):
        m = series_from_rows([_row(unit=""), _row(ts="2026-09-22T14:01:00Z", unit="W")])
        assert m.series[0].unit == "W"

    def test_two_units_under_one_name_are_COUNTED_not_resolved(self):
        """A conformance fault. Taking the last one seen would hide it behind a
        figure that looks fine."""
        m = series_from_rows([_row(unit="W"), _row(ts="2026-09-22T14:01:00Z", unit="kW")])
        assert m.conflicting_units == ("Power",)

    def test_one_unit_throughout_is_not_a_conflict(self):
        m = series_from_rows([_row(), _row(ts="2026-09-22T14:01:00Z")])
        assert m.conflicting_units == ()

    def test_no_unit_at_all_stays_no_unit(self):
        """The renderer says "unit not declared" rather than assuming one."""
        assert series_from_rows([_row(unit=None)]).series[0].unit == ""


class TestTheShapeIsTheCallersBusiness:
    def test_column_names_can_be_given(self):
        m = series_from_rows(
            [{"t": "2026-09-22T14:00:00Z", "v": 2.0, "name": "a", "u": "W"}],
            time_column="t", value_column="v", series_column="name", unit_column="u",
        )
        assert m.series[0].name == "a" and m.series[0].unit == "W"

    def test_a_datetime_is_taken_as_it_stands(self):
        stamp = datetime(2026, 9, 22, 14, tzinfo=UTC)
        assert series_from_rows([_row(ts=stamp)]).series[0].points[0][0] == stamp

    def test_a_naive_datetime_is_read_as_utc_rather_than_refused(self):
        m = series_from_rows([_row(ts=datetime(2026, 9, 22, 14))])
        assert m.series[0].points[0][0].tzinfo is not None


class TestItIsOrdered:
    def test_points_come_out_in_time_order_whatever_order_they_arrived(self):
        m = series_from_rows([
            _row(ts="2026-09-22T14:02:00Z", value=3.0),
            _row(ts="2026-09-22T14:00:00Z", value=1.0),
            _row(ts="2026-09-22T14:01:00Z", value=2.0),
        ])
        assert [v for _t, v in m.series[0].points] == [1.0, 2.0, 3.0]

    def test_channels_come_out_in_name_order(self):
        m = series_from_rows([_row(channel="b"), _row(channel="a")])
        assert [s.name for s in m.series] == ["a", "b"]

    def test_and_the_models_come_after_the_measurements(self):
        m = series_from_rows([_row(channel="z", source_class="predicted"), _row(channel="a")])
        assert [s.modelled for s in m.series] == [False, True]


class TestNothingDrawable:
    def test_is_falsey_rather_than_an_empty_figure(self):
        assert not series_from_rows([])
        assert not series_from_rows([_row(ts="nope")])

    def test_but_still_reports_what_it_saw(self):
        assert series_from_rows([_row(ts="nope")]).unusable == 1


class TestAnInstantCanBeANumber:
    """A bucketed query returns `extract(epoch from ts)`, which is a float. The
    first cut read only strings and datetimes, so a real query drew nothing at
    all: every row counted as unusable and the figure came back empty."""

    def test_an_epoch_in_seconds_is_a_time(self):
        m = series_from_rows([_row(ts=1790341392.854)])
        assert m.unusable == 0
        assert m.series[0].points[0][0] == datetime.fromtimestamp(1790341392.854, tz=UTC)

    def test_an_integer_epoch_too(self):
        assert series_from_rows([_row(ts=1790341392)]).unusable == 0

    def test_it_is_not_guessed_between_seconds_and_milliseconds(self):
        """That heuristic is wrong once and then invisible. Seconds, said out
        loud, and a caller holding milliseconds knows it."""
        seconds = series_from_rows([_row(ts=1_790_341_392)]).series[0].points[0][0]
        assert seconds.year == 2026

    def test_a_number_that_is_no_time_at_all_is_still_unusable(self):
        assert series_from_rows([_row(ts=1e30)]).unusable == 1
