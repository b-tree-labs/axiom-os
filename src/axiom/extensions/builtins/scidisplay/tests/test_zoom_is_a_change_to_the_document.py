# Copyright (c) 2026 B-Tree Ventures, LLC
# SPDX-License-Identifier: Apache-2.0
"""Zooming, panning, and what a chart shows before anybody has zoomed.

A surface could scale the picture it already has. That is the cheap
implementation and it is wrong three ways, each of which shows up the first
time somebody uses it: it shows no more detail, because the marks were already
bucketed; it cannot be shared, because the window is what the figure is OF; and
every surface would have to mean something different by "zoom in".

So zoom takes a window and returns a window, the document carries it, and the
bucket is re-chosen every time. These tests are about the consequences: that a
narrower window really does earn finer marks, that the answer is the same on
every surface, and that the two things zoom refuses to do are refused out loud.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from axiom.extensions.builtins.scidisplay.chart_spec import (
    ChartSpec,
    Transform,
    Window,
    parse_json,
)
from axiom.extensions.builtins.scidisplay.chart_window import (
    BUCKET_LADDER,
    DEFAULT_SPAN,
    MIN_MARKS,
    ZOOM_STEP,
    Bounds,
    WindowError,
    bucket_seconds,
    choose_bucket,
    default_window,
    pan,
    rebucketed,
    span_seconds,
    window_bounds,
    zoom,
)

EARLIEST = datetime(2026, 9, 1, tzinfo=UTC)
LATEST = datetime(2026, 9, 26, tzinfo=UTC)
BOUNDS = Bounds(EARLIEST, LATEST)


def _win(start: datetime, end: datetime) -> Window:
    return Window(fields={"from": start.isoformat().replace("+00:00", "Z"),
                          "to": end.isoformat().replace("+00:00", "Z")})


def _spec(window: Window, bucket: str = "1h") -> ChartSpec:
    return ChartSpec(kind="timeseries", site="s", channels=("a",), window=window,
                     transform=Transform(bucket=bucket, agg="mean"), title="t")


class TestTheWindowIsTheFigure:
    def test_zooming_in_narrows_it(self):
        before = _win(EARLIEST, LATEST)
        after, _ = zoom(before, bounds=BOUNDS)
        assert span_seconds(after) == pytest.approx(span_seconds(before) / ZOOM_STEP)

    def test_zooming_out_widens_it(self):
        before, _ = default_window(BOUNDS)
        after, _ = zoom(before, factor=1 / ZOOM_STEP, bounds=BOUNDS)
        assert span_seconds(after) > span_seconds(before)

    def test_in_then_out_returns_where_it_started(self):
        before, _ = default_window(BOUNDS)
        once, _ = zoom(before, bounds=BOUNDS)
        back, _ = zoom(once, factor=1 / ZOOM_STEP, bounds=BOUNDS)
        assert window_bounds(back) == window_bounds(before)

    def test_the_same_window_and_the_same_notch_give_the_same_answer(self):
        """Which is what lets a terminal, a chat and a page agree about what
        "zoom in" means."""
        before, _ = default_window(BOUNDS)
        assert zoom(before, bounds=BOUNDS)[0] == zoom(before, bounds=BOUNDS)[0]

    def test_a_zoomed_window_still_round_trips_through_a_document(self):
        after, _ = zoom(_win(EARLIEST, LATEST), bounds=BOUNDS)
        spec = _spec(after)
        assert parse_json(spec.to_json()) == spec


class TestZoomingInEarnsFinerMarks:
    """The reason zoom belongs in the document. A viewport transform over
    marks already bucketed at an hour draws the same hourly marks larger, and
    the reader learns nothing."""

    def test_the_bucket_gets_finer(self):
        wide = _spec(_win(EARLIEST, LATEST))
        narrow, _ = zoom(wide.window, factor=64, bounds=BOUNDS)
        after = rebucketed(wide, narrow)
        assert bucket_seconds(after.transform.bucket) < bucket_seconds(
            wide.transform.bucket
        )

    def test_and_the_mark_count_stays_in_the_same_range(self):
        """A ten-minute chart and a ten-month chart plot the same number of
        marks, which is what keeps both legible."""
        window = _win(EARLIEST, LATEST)
        spec = rebucketed(_spec(window), window)
        for _ in range(8):
            window, _ = zoom(window, factor=4, bounds=BOUNDS)
            spec = rebucketed(spec, window)
            marks = span_seconds(window) / bucket_seconds(spec.transform.bucket)
            assert 20 <= marks <= 1200, f"{marks:.0f} marks at {spec.transform.bucket}"

    def test_a_kind_with_no_transform_keeps_none(self):
        """A state has spans, and bucketing one by mean is the meaningless
        operation that kind exists to avoid."""
        spec = ChartSpec(kind="state", site="s", channels=("Mode",),
                         window=_win(EARLIEST, LATEST), title="t")
        narrow, _ = zoom(spec.window, bounds=BOUNDS)
        assert rebucketed(spec, narrow).transform is None

    def test_the_spec_id_changes_so_the_figure_can_be_cited(self):
        wide = rebucketed(_spec(_win(EARLIEST, LATEST)), _win(EARLIEST, LATEST))
        narrow, _ = zoom(wide.window, bounds=BOUNDS)
        assert rebucketed(wide, narrow).spec_id != wide.spec_id


class TestZoomingTowardsAPoint:
    def test_the_instant_under_the_pointer_holds_still(self):
        """Which is what makes a wheel feel like it is zooming towards
        something rather than towards the middle."""
        before = _win(EARLIEST, LATEST)
        at = EARLIEST + timedelta(days=5)
        after, _ = zoom(before, factor=4, at=at, bounds=BOUNDS)
        start, end = window_bounds(after)
        fraction = (at - start) / (end - start)
        was = (at - EARLIEST) / (LATEST - EARLIEST)
        assert fraction == pytest.approx(was, abs=0.02)

    def test_without_a_point_the_middle_holds_still(self):
        before = _win(EARLIEST, LATEST)
        after, _ = zoom(before, factor=4, bounds=BOUNDS)
        start, end = window_bounds(after)
        middle = EARLIEST + (LATEST - EARLIEST) / 2
        assert start + (end - start) / 2 == middle


class TestWhatItRefusesToDo:
    def test_it_will_not_zoom_out_past_the_readings(self):
        """Empty space either side of a series looks like a gap in the
        readings."""
        after, notes = zoom(_win(EARLIEST, LATEST), factor=0.01, bounds=BOUNDS)
        assert window_bounds(after) == (EARLIEST, LATEST)
        assert any("as far out" in n for n in notes)

    def test_it_will_not_zoom_in_below_what_the_readings_resolve(self):
        after, notes = zoom(_win(EARLIEST, LATEST), factor=1e9, bounds=BOUNDS)
        assert span_seconds(after) >= MIN_MARKS * 0.1
        assert any(str(MIN_MARKS) in n for n in notes)

    def test_and_says_so_rather_than_doing_nothing(self):
        """A control that silently does nothing is one a person keeps
        pressing."""
        _, notes = zoom(_win(EARLIEST, LATEST), factor=0.01, bounds=BOUNDS)
        assert notes

    def test_panning_stops_at_the_edge_and_keeps_its_span(self):
        window, _ = default_window(BOUNDS)
        moved, notes = pan(window, fraction=100, bounds=BOUNDS)
        assert span_seconds(moved) == pytest.approx(span_seconds(window))
        assert window_bounds(moved)[1] == LATEST
        assert any("last reading" in n for n in notes)

    def test_panning_back_stops_at_the_first_reading(self):
        window, _ = default_window(BOUNDS)
        moved, notes = pan(window, fraction=-100, bounds=BOUNDS)
        assert window_bounds(moved)[0] == EARLIEST
        assert any("first reading" in n for n in notes)

    def test_a_run_relative_window_is_refused_by_name(self):
        """It is measured from an event this cannot see, so it is refused
        rather than guessed at."""
        window = Window(basis="run_relative",
                        fields={"run": "r1", "from": "+0s", "to": "+10m"})
        with pytest.raises(WindowError, match="run_relative"):
            zoom(window, bounds=BOUNDS)

    def test_a_negative_factor_is_refused(self):
        with pytest.raises(WindowError, match="positive"):
            zoom(_win(EARLIEST, LATEST), factor=-2)

    def test_an_unreadable_window_is_refused(self):
        """A validated `Window` cannot hold these, so this guards the case
        where a window arrives as a plain table from somewhere else."""

        class _Whatever:
            basis = "absolute"
            fields = {"from": "not a time", "to": "nor this"}

        with pytest.raises(WindowError, match="readable"):
            zoom(_Whatever())


class TestTheDefaultWindow:
    def test_it_is_the_most_recent_span_of_the_readings(self):
        window, _ = default_window(BOUNDS)
        assert window_bounds(window) == (LATEST - DEFAULT_SPAN, LATEST)

    def test_it_is_anchored_to_the_LAST_READING_not_to_the_clock(self):
        """A window measured from now is empty the moment ingestion stops, and
        an empty chart looks exactly like a working chart of a quiet signal."""
        long_ago = Bounds(datetime(2019, 1, 1, tzinfo=UTC),
                          datetime(2019, 6, 1, tzinfo=UTC))
        window, _ = default_window(long_ago)
        assert window_bounds(window)[1] == long_ago.latest

    def test_and_says_how_stale_that_is(self):
        """So a feed that died is visible as a figure that ends, rather than
        as a blank."""
        long_ago = Bounds(datetime(2019, 1, 1, tzinfo=UTC),
                          datetime(2019, 6, 1, tzinfo=UTC))
        _, notes = default_window(long_ago)
        assert any("old" in n for n in notes)

    def test_shorter_data_is_shown_whole_rather_than_padded(self):
        """Empty space either side of a series reads as a gap in the
        readings."""
        brief = Bounds(EARLIEST, EARLIEST + timedelta(minutes=40))
        window, notes = default_window(brief)
        assert window_bounds(window) == (brief.earliest, brief.latest)
        assert notes == []

    def test_the_span_is_a_parameter_not_a_law(self):
        window, _ = default_window(BOUNDS, span=timedelta(hours=6))
        assert span_seconds(window) == 6 * 3600


class TestTheBucketLadder:
    def test_a_bucket_is_one_a_person_recognises(self):
        """"5m" reads; "3.7m" does not, and a spec is meant to be read as much
        as executed."""
        rungs = {name for name, _seconds in BUCKET_LADDER}
        for span in (60, 600, 3600, 86400, 86400 * 30):
            assert choose_bucket(span) in rungs

    def test_a_window_with_no_span_cannot_be_bucketed(self):
        with pytest.raises(WindowError, match="no span"):
            choose_bucket(0)


class TestAFigureOpensWhereItsSeriesMeet:
    """A comparison used to open on the union of its series and draw one line.

    `measured_cm` stops on 10 April and `predicted_cm` runs to the 12th. The
    default window is the most recent day of the union, which is the 11th to
    the 12th — a span in which the measurement does not exist. The reader
    asked to compare a measurement against a model of it and got a figure of
    the model alone, with nothing saying the other line had been left behind.
    """

    from datetime import UTC as _UTC
    from datetime import datetime as _dt

    M = (_dt(2025, 1, 8, tzinfo=_UTC), _dt(2026, 4, 10, tzinfo=_UTC))
    P = (_dt(2025, 1, 3, tzinfo=_UTC), _dt(2026, 4, 12, tzinfo=_UTC))

    def test_one_series_opens_on_everything_it_has(self):
        from axiom.extensions.builtins.scidisplay.chart_window import drawable_bounds

        got = drawable_bounds({"measured_cm": self.M})
        assert (got.bounds.earliest, got.bounds.latest) == self.M
        assert got.notes == ()

    def test_two_series_open_where_both_have_readings(self):
        from axiom.extensions.builtins.scidisplay.chart_window import drawable_bounds

        got = drawable_bounds({"measured_cm": self.M, "predicted_cm": self.P})
        assert got.bounds.earliest == self.M[0], "opened before the measurement starts"
        assert got.bounds.latest == self.M[1], "opened past where the measurement stops"

    def test_and_say_which_series_run_past_that(self):
        from axiom.extensions.builtins.scidisplay.chart_window import drawable_bounds

        got = drawable_bounds({"measured_cm": self.M, "predicted_cm": self.P})
        assert "predicted_cm runs beyond it" in " ".join(got.notes)

    def test_series_that_never_met_are_told_so_rather_than_shown_apart(self):
        """Not a failure to answer: it IS the answer. Two things that were
        never recorded at the same time cannot be compared, and a reader
        squinting at two lines in opposite halves of a plot should be told
        that rather than left to work it out."""
        from datetime import UTC, datetime

        from axiom.extensions.builtins.scidisplay.chart_window import drawable_bounds

        old = (datetime(2020, 1, 1, tzinfo=UTC), datetime(2020, 2, 1, tzinfo=UTC))
        got = drawable_bounds({"measured_cm": self.M, "ancient": old})
        assert got.disjoint
        assert got.bounds.earliest == old[0] and got.bounds.latest == self.M[1]
        assert "never recorded at the same time" in " ".join(got.notes)

    def test_nothing_to_draw_is_refused_by_name(self):
        import pytest as _pytest

        from axiom.extensions.builtins.scidisplay.chart_window import (
            WindowError,
            drawable_bounds,
        )

        with _pytest.raises(WindowError):
            drawable_bounds({})


class TestAWindowSomebodyTypedIsStillAWindow:
    """A date picker could ask for nine months over a record of twenty-five
    days. The figure drew it: twenty-five days of readings inside an
    eight-month frame, seven-eighths of it empty, and a row of spans offering
    "last 15 minutes" because nothing longer than a week exists to offer.

    The view said months and the record said weeks, and nothing reconciled
    them. Zoom and pan have always been clamped; a typed window is the same
    kind of thing.
    """

    from datetime import UTC as _UTC
    from datetime import datetime as _dt

    RECORD = None  # set in each test, so the class reads top to bottom

    def _bounds(self):
        from axiom.extensions.builtins.scidisplay.chart_window import Bounds

        return Bounds(self._dt(2026, 8, 31, tzinfo=self._UTC),
                      self._dt(2026, 9, 26, tzinfo=self._UTC))

    def _asked(self, start, end):
        return _win(start, end)

    def test_a_window_wider_than_the_record_becomes_the_record(self):
        from axiom.extensions.builtins.scidisplay.chart_window import (
            settle,
            window_bounds,
        )

        asked = self._asked(self._dt(2026, 1, 7, tzinfo=self._UTC),
                            self._dt(2026, 9, 26, tzinfo=self._UTC))
        got, notes = settle(asked, bounds=self._bounds())
        start, end = window_bounds(got)
        assert (start, end) == (self._bounds().earliest, self._bounds().latest)
        assert "as far out as the readings go" in notes

    def test_a_window_that_starts_before_the_readings_slides_to_them(self):
        from axiom.extensions.builtins.scidisplay.chart_window import (
            settle,
            window_bounds,
        )

        asked = self._asked(self._dt(2026, 8, 1, tzinfo=self._UTC),
                            self._dt(2026, 8, 11, tzinfo=self._UTC))
        got, notes = settle(asked, bounds=self._bounds())
        start, end = window_bounds(got)
        assert start == self._bounds().earliest
        assert (end - start).days == 10, "the span the reader asked for is kept"
        assert "at the first reading" in notes

    def test_a_window_inside_the_readings_is_left_alone(self):
        from axiom.extensions.builtins.scidisplay.chart_window import (
            settle,
            window_bounds,
        )

        a = self._dt(2026, 9, 1, tzinfo=self._UTC)
        b = self._dt(2026, 9, 8, tzinfo=self._UTC)
        got, notes = settle(self._asked(a, b), bounds=self._bounds())
        assert window_bounds(got) == (a, b)
        assert notes == []

    def test_it_says_the_same_words_a_zoom_to_the_edge_says(self):
        """However a reader arrived at the edge, the figure says so the same
        way. Two vocabularies for one fact is a fact that has to be learned
        twice."""
        from axiom.extensions.builtins.scidisplay.chart_window import settle, zoom

        bounds = self._bounds()
        wide = self._asked(self._dt(2026, 1, 7, tzinfo=self._UTC),
                           self._dt(2026, 9, 26, tzinfo=self._UTC))
        _typed, typed_notes = settle(wide, bounds=bounds)
        inside = self._asked(self._dt(2026, 9, 1, tzinfo=self._UTC),
                             self._dt(2026, 9, 8, tzinfo=self._UTC))
        _zoomed, zoom_notes = zoom(inside, factor=0.01, bounds=bounds)
        assert typed_notes == zoom_notes


class TestHowWideAFigureOpens:
    """`corrected_cm` holds 397 readings over 464 days — about one a day. It
    opened on the most recent 24 hours of that and drew ONE POINT.

    The window was a constant, and one constant cannot be right for both a
    thermocouple sampled twice a second and a rod position measured once a
    day. "Why did we pick one day as a default time window for this
    measurement" has no good answer, which is the problem.
    """

    from datetime import UTC as _UTC
    from datetime import datetime as _dt
    from datetime import timedelta as _td

    def _bounds(self, days):
        from axiom.extensions.builtins.scidisplay.chart_window import Bounds

        end = self._dt(2026, 4, 12, tzinfo=self._UTC)
        return Bounds(end - self._td(days=days), end)

    def test_a_once_a_day_channel_opens_on_its_whole_record(self):
        from axiom.extensions.builtins.scidisplay.chart_window import (
            cadence_of,
            default_window,
            span_seconds,
        )

        bounds = self._bounds(464)
        cadence = cadence_of(397, bounds.span)
        window, _notes = default_window(bounds, cadence=cadence)
        assert span_seconds(window) == bounds.span, "still opens on a single day"

    def test_a_twice_a_second_channel_keeps_the_day(self):
        """A window sized to hold 800 readings of a 2 Hz signal is seven
        minutes, and nobody opening a figure wants to start there. The rule
        only ever widens."""
        from axiom.extensions.builtins.scidisplay.chart_window import (
            DEFAULT_SPAN,
            cadence_of,
            default_window,
            span_seconds,
        )

        bounds = self._bounds(632)
        cadence = cadence_of(632 * 86400 * 2, bounds.span)
        window, _notes = default_window(bounds, cadence=cadence)
        assert span_seconds(window) == DEFAULT_SPAN.total_seconds()

    def test_a_five_minute_channel_opens_on_days_rather_than_one(self):
        from axiom.extensions.builtins.scidisplay.chart_window import (
            cadence_of,
            default_window,
            span_seconds,
        )

        bounds = self._bounds(400)
        cadence = cadence_of(int(400 * 24 * 12), bounds.span)  # every 5 minutes
        window, _notes = default_window(bounds, cadence=cadence)
        assert 2 * 86400 < span_seconds(window) < 4 * 86400

    def test_an_unknown_cadence_changes_nothing(self):
        from axiom.extensions.builtins.scidisplay.chart_window import (
            DEFAULT_SPAN,
            cadence_of,
            default_window,
            span_seconds,
        )

        bounds = self._bounds(464)
        assert cadence_of(1, bounds.span) == 0.0
        assert cadence_of(500, 0) == 0.0
        window, _notes = default_window(bounds, cadence=0.0)
        assert span_seconds(window) == DEFAULT_SPAN.total_seconds()
