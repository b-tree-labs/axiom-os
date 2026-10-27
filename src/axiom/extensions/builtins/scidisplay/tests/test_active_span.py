# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Framing a chart on when the signal was doing something.

Ben, 2026-09-30, on the surface we are replacing: *"the time window that's
defaulted to is always preset to be when the data showed activity. So it didn't
default to like midnight where for the next eight hours there's no data … in
general, when showing a graph, we need to show whatever is recent and has data
… ignore the long periods of no data."*

The numbers below are from a real operating day, 1,441 console readings on
2026-09-10. The window a DECLARED power threshold produced was 07:54 to 15:23.
This module knows nothing about reactors, power or thresholds, and lands on the
same two instants.
"""

from __future__ import annotations

import datetime as dt

from axiom.extensions.builtins.scidisplay.chart_window import (
    ACTIVE_FRACTION,
    active_span,
)

DAY = dt.datetime(2026, 9, 10, tzinfo=dt.UTC)


def series(*, idle: float, peak: float, on_from: float, on_to: float, n: int = 1440):
    """A day of readings that are idle, then active, then idle again."""
    out = []
    for i in range(n):
        at = DAY + dt.timedelta(minutes=i)
        h = i / 60
        out.append((at, peak if on_from <= h <= on_to else idle))
    return out


class TestItFindsTheActivityAndNotTheCalendarDay:
    def test_a_day_that_is_mostly_idle_frames_the_active_part(self):
        got = active_span(series(idle=0.000182, peak=1_080_000, on_from=8, on_to=15), lead_in=0)
        assert got is not None
        assert got[0].hour == 8
        assert got[1].hour == 15

    def test_the_span_is_about_three_times_denser_than_the_day(self):
        """The measured gain on the real day, asserted rather than left in
        prose: 7.5 hours of 24."""
        got = active_span(series(idle=0.000182, peak=1_080_000, on_from=8, on_to=15), lead_in=0)
        hours = (got[1] - got[0]).total_seconds() / 3600
        assert 2.5 <= 24 / hours <= 3.5

    def test_presence_of_readings_would_have_found_nothing(self):
        """Why this measures the VALUE and not whether a reading arrived. The
        console reports every minute for 24 hours whether or not the reactor is
        on, so a gap-based rule sees one unbroken run and narrows nothing."""
        points = series(idle=0.000182, peak=1_080_000, on_from=8, on_to=15)
        gaps = {(b[0] - a[0]).total_seconds() for a, b in zip(points, points[1:])}
        assert gaps == {60.0}  # no gap anywhere to key on


class TestTheLeadIn:
    def test_it_starts_a_little_before_the_activity(self):
        """Ben: "highlight the beginning and maybe a little bit before". A window
        starting exactly at the first active reading clips the rise, and the rise
        is what a reader is looking for."""
        points = series(idle=0.000182, peak=1_080_000, on_from=8, on_to=15)
        bare = active_span(points, lead_in=0)
        led = active_span(points, lead_in=0.05)
        assert led[0] < bare[0]
        assert led[1] == bare[1]  # only the start moves

    def test_it_never_reaches_before_the_record(self):
        """Padding into a period with no readings frames emptiness, which is the
        thing this exists to avoid."""
        points = series(idle=0.0, peak=100.0, on_from=0, on_to=2)
        got = active_span(points, lead_in=0.5)
        assert got[0] == points[0][0]


class TestWhatItRefusesToGuess:
    def test_a_flat_channel_has_no_activity_to_frame(self):
        """`None`, not the whole record. "Nothing happened" and "something
        happened throughout" are different answers and a caller may want to say
        so differently."""
        flat = [(DAY + dt.timedelta(minutes=i), 17.0) for i in range(1440)]
        assert active_span(flat) is None

    def test_no_readings_is_not_an_error(self):
        assert active_span([]) is None

    def test_a_none_value_is_skipped_rather_than_read_as_zero(self):
        """Absence is not a reading of zero, and treating it as one would invent
        a departure from the quiet level."""
        points = [(DAY + dt.timedelta(minutes=i), None) for i in range(10)]
        assert active_span(points) is None


class TestItNeedsNoDeclaredThreshold:
    def test_the_answer_barely_depends_on_the_fraction(self):
        """Measured on the real day: every fraction from 0.0001 to 0.05 — a 500x
        range — gives the same window to within one minute. That insensitivity
        is what makes a relative constant safe here instead of a guess."""
        points = series(idle=0.000182, peak=1_080_000, on_from=8, on_to=15)
        spans = {active_span(points, fraction=f, lead_in=0) for f in (0.0001, 0.001, 0.01, 0.05)}
        starts = {s[0] for s in spans}
        assert len(starts) == 1

    def test_it_works_across_wildly_different_magnitudes(self):
        """Watts spanning six decades and degrees spanning seven units go
        through the same rule with no per-channel configuration."""
        watts = active_span(series(idle=0.000182, peak=1_080_000, on_from=8, on_to=15), lead_in=0)
        degrees = active_span(series(idle=17.0, peak=386.0, on_from=8, on_to=15), lead_in=0)
        assert watts == degrees

    def test_a_dip_from_a_high_resting_level_is_activity_too(self):
        """`abs(value - quiet)`, so a signal that falls is as active as one that
        rises. Keying on "above the quiet level" would be simpler and would
        silently miss half the shapes a reader cares about."""
        dipping = [
            (DAY + dt.timedelta(minutes=i), 5.0 if 480 <= i <= 900 else 100.0) for i in range(1440)
        ]
        got = active_span(dipping, lead_in=0)
        assert got is not None
        assert (got[1] - got[0]).total_seconds() < 8 * 3600

    def test_the_default_fraction_is_the_measured_one(self):
        assert ACTIVE_FRACTION == 0.001


# --------------------------------------------------------------------------
# Two defects the first version had, both found by pointing it at channels
# other than the one it was tuned on.
#
# It was measured against a channel that rests at ~0 and peaks six decades
# higher, where a thousandth of the range is a large multiple of the resting
# spread and every departure is enormous. Neither of those holds in general,
# and where they fail the window widens back to the whole calendar day — which
# is the exact outcome this feature exists to avoid.
#
# The numbers in each test below are from the same real day, on channels the
# first version got wrong.
# --------------------------------------------------------------------------


def wandering_rest(*, rest: float, spread: float, n: int = 1440):
    """A channel whose resting level is not a constant.

    Measured shape: a temperature resting between 15 and 18 with a median of
    17.1 and a spread of 1.4, on a day whose peak is 378.8. A thousandth of
    that range is 0.38 — well UNDER the resting wander, so every resting
    sample reads as a departure and the window becomes the whole day.
    """
    out = []
    for i in range(n):
        at = DAY + dt.timedelta(minutes=i)
        out.append((at, rest + ((i * 7) % 11 - 5) / 5 * spread))
    return out


class TestARestingLevelIsNotAConstant:
    def test_a_channel_that_wanders_at_rest_still_frames_its_excursion(self):
        """The defect, stated as the fix: 15-18 at rest, one excursion to 378
        between 10:25 and 11:30. The window must be the excursion, not the day."""
        points = wandering_rest(rest=17.1, spread=1.4)
        for i in range(625, 691):  # 10:25 to 11:30
            points[i] = (points[i][0], 378.8)
        got = active_span(points, lead_in=0)
        assert got is not None
        assert got[0].hour == 10 and got[0].minute == 25
        assert got[1].hour == 11 and got[1].minute == 30

    def test_the_threshold_clears_the_channels_own_resting_spread(self):
        """Stated as the invariant rather than as a window, because this is the
        thing that has to hold for every channel and not just the measured one:
        no resting sample may count as a departure."""
        points = wandering_rest(rest=17.1, spread=1.4)
        for i in range(625, 691):
            points[i] = (points[i][0], 378.8)
        got = active_span(points, lead_in=0)
        assert (got[1] - got[0]).total_seconds() < 2 * 3600

    def test_it_holds_across_a_wide_band_of_the_multiple(self):
        """Measured: every multiple from 2 to 8 gives the same window on the
        real day. A constant that only works at one value is a fitted
        parameter; one that works across a factor of four is a rule."""
        points = wandering_rest(rest=17.1, spread=1.4)
        for i in range(625, 691):
            points[i] = (points[i][0], 378.8)
        spans = {
            active_span(points, resting_multiple=k, lead_in=0) for k in (2.0, 3.0, 4.0, 6.0, 8.0)
        }
        assert len(spans) == 1


class TestOneStraySampleDoesNotSetTheEdge:
    def test_isolated_samples_near_each_end_do_not_stretch_the_window(self):
        """Measured: a channel resting at exactly 0 all day, with real activity
        from 07:54 to 15:22 and single one-minute samples of 1.0 at 01:14 and
        23:30. The first version returned 01:14 to 23:30 — twenty-two hours of
        which two contained readings that mattered.

        The real runs were 22 to 88 minutes; the strays were one sample. That
        is a separation of twenty-fold, where their magnitudes differ only
        five-fold, which is why the rule is about duration first.
        """
        points = [(DAY + dt.timedelta(minutes=i), 0.0) for i in range(1440)]
        for i in range(474, 923):  # 07:54 to 15:22
            points[i] = (points[i][0], 100.0)
        points[74] = (points[74][0], 1.0)  # 01:14
        points[1410] = (points[1410][0], 1.0)  # 23:30
        got = active_span(points, lead_in=0)
        assert got is not None
        assert got[0].hour == 7 and got[0].minute == 54
        assert got[1].hour == 15 and got[1].minute == 22

    def test_a_sample_is_a_stray_only_in_the_company_of_something_bigger(self):
        """Written as a failing assertion first, and the failure was right.

        The same three one-minute samples, with nothing else in the record, are
        kept — and they should be. "Stray" is not a property a sample has on its
        own; magnitude here is measured against the largest departure present,
        so with nothing larger present those samples ARE the activity, and the
        only other answer a chart could give is the same day with nothing
        pointed out in it.

        The test above is the same shape with one thing added: a hundred-fold
        larger run in the middle of the day. That is what makes the samples at
        the edges strays, and it is the only thing that does.
        """
        points = [(DAY + dt.timedelta(minutes=i), 0.0) for i in range(1440)]
        for i in (74, 196, 1410):
            points[i] = (points[i][0], 1.0)
        got = active_span(points, lead_in=0)
        assert got is not None
        assert got[0].hour == 1 and got[0].minute == 14

    def test_and_it_becomes_a_stray_the_moment_something_bigger_arrives(self):
        """The pair to the one above: nothing about the samples changed."""
        points = [(DAY + dt.timedelta(minutes=i), 0.0) for i in range(1440)]
        for i in (74, 196, 1410):
            points[i] = (points[i][0], 1.0)
        before = active_span(points, lead_in=0)
        for i in range(600, 700):
            points[i] = (points[i][0], 100.0)
        after = active_span(points, lead_in=0)
        assert before[0].hour == 1
        assert after[0].hour == 10 and after[1].hour == 11


class TestABriefEventIsNotAStraySample:
    def test_a_single_sample_of_large_amplitude_is_kept(self):
        """This is the case that forbids a pure duration rule.

        A pulse is over in milliseconds, so at any bucket a chart would probe
        with it is ONE sample — the same width as the stray above, and the most
        important thing in the record. So a short run survives on magnitude:
        a stray is brief AND small, an event is brief OR large.
        """
        points = [(DAY + dt.timedelta(minutes=i), 0.0) for i in range(1440)]
        points[600] = (points[600][0], 1_000_000.0)
        got = active_span(points, lead_in=0)
        assert got is not None
        assert got[0].hour == 10 and got[0].minute == 0

    def test_a_pulse_survives_beside_a_long_ordinary_run(self):
        """The pulse is 1/1000th the length of the steady run and ten times its
        amplitude. Both belong on screen."""
        points = [(DAY + dt.timedelta(minutes=i), 0.0) for i in range(1440)]
        for i in range(474, 923):
            points[i] = (points[i][0], 100.0)
        points[1200] = (points[1200][0], 1_000.0)  # 20:00
        got = active_span(points, lead_in=0)
        assert got[1].hour == 20


class TestWhatNoneMeansNow:
    def test_a_channel_with_no_stand_out_stretch_returns_none(self):
        """Sharpened from "never departs from its quiet level".

        A channel wandering continuously has no stretch that stands out from
        the rest of its record, so there is nothing to frame — and `None` tells
        the caller to show what it would have shown anyway, which is the whole
        record. The picture a reader gets is unchanged from when this returned
        that whole span itself; what changed is that the answer no longer
        claims to have FOUND something.
        """
        wander = [(DAY + dt.timedelta(minutes=i), 19 + (i % 70) / 10) for i in range(1440)]
        assert active_span(wander) is None


class TestARecordOfOneInstantStillDraws:
    """Found by pointing the surface at a feed with one reading in it.

    `default_window` hands back the whole record when the record is shorter
    than the span asked for, and a record of ONE reading is a window whose
    ends are the same instant. Nothing downstream accepts that: the spec
    refuses `from == to`, so the figure lane answered 500 — on a channel whose
    only fault was being new.
    """

    def test_a_single_reading_gets_a_drawable_window(self):
        from axiom.extensions.builtins.scidisplay.chart_window import (
            Bounds,
            default_window,
            window_bounds,
        )

        at = dt.datetime(2026, 9, 4, 9, 5, tzinfo=dt.timezone.utc)
        window, _notes = default_window(Bounds(at, at))
        start, end = window_bounds(window)
        assert end > start

    def test_it_is_centred_on_the_reading_rather_than_starting_at_it(self):
        """The reading is what the reader came for, so it belongs in the
        middle of the frame rather than on its edge."""
        from axiom.extensions.builtins.scidisplay.chart_window import (
            Bounds,
            default_window,
            window_bounds,
        )

        at = dt.datetime(2026, 9, 4, 9, 5, tzinfo=dt.timezone.utc)
        start, end = window_bounds(default_window(Bounds(at, at))[0])
        assert start < at < end
        before, after = (at - start).total_seconds(), (end - at).total_seconds()
        assert abs(before - after) < 1e-6

    def test_a_record_with_width_is_untouched(self):
        from axiom.extensions.builtins.scidisplay.chart_window import (
            Bounds,
            default_window,
            window_bounds,
        )

        lo = dt.datetime(2026, 9, 4, tzinfo=dt.timezone.utc)
        hi = lo + dt.timedelta(hours=3)
        start, end = window_bounds(default_window(Bounds(lo, hi))[0])
        assert (start, end) == (lo, hi)
