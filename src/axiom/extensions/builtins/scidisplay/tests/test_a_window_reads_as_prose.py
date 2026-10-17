# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0
"""The window under a figure, written for a person.

Ben: "the text at the bottom of the graph is hard to read, especially that
date format ... which I think is a date range with the full date format.
It's very difficult to read."

It was a pair of ISO timestamps with microseconds — sixty characters
carrying the date twice, the offset twice, and a precision the figure does
not have, since it is drawn from buckets.
"""

from __future__ import annotations

import datetime as dt

import pytest

from axiom.extensions.builtins.scidisplay.moment_format import format_range, zone_of

A = dt.datetime(2026, 9, 4, 4, 35, 57, 936000, tzinfo=dt.UTC)


class TestItSaysEachThingOnce:
    def test_a_window_inside_one_day_names_that_day_once(self):
        out = format_range(A, dt.datetime(2026, 9, 4, 4, 37, 36, 588000, tzinfo=dt.UTC))
        assert out == "4 Sep 2026, 04:35:57–04:37:37 UTC"
        assert out.count("2026") == 1
        assert out.count("Sep") == 1
        assert out.count("UTC") == 1

    def test_and_crossing_one_gives_each_end_its_own(self):
        out = format_range(A, dt.datetime(2026, 9, 6, 6, 10, tzinfo=dt.UTC))
        assert out == "4 Sep 2026 04:35 – 6 Sep 2026 06:10 UTC"

    def test_no_microseconds_survive(self):
        # They are where a bucket happened to start, not when anything was
        # measured. Printing them claims a precision the figure does not have.
        assert ".936" not in format_range(A, A + dt.timedelta(minutes=2))

    def test_it_is_far_shorter_than_what_it_replaced(self):
        end = dt.datetime(2026, 9, 4, 4, 37, 36, 588000, tzinfo=dt.UTC)
        iso = f"{A.isoformat()}–{end.isoformat()}"
        assert len(format_range(A, end)) < len(iso) / 1.7


class TestItNeverShowsTheWindowNarrowerThanItIs:
    """A window shown narrower than it is looks like it excludes a reading
    the figure drew, and a reader comparing line to label would be right to
    think something was wrong. So it rounds outwards."""

    def test_the_start_floors_and_the_end_ceils(self):
        out = format_range(A, dt.datetime(2026, 9, 4, 4, 37, 36, 1, tzinfo=dt.UTC))
        assert out == "4 Sep 2026, 04:35:57–04:37:37 UTC"

    def test_at_whatever_resolution_is_shown(self):
        # Minutes, over a span of days: the seconds are gone, so the floor
        # and ceiling move to the minute rather than staying on the second.
        out = format_range(A, dt.datetime(2026, 9, 9, 6, 10, 0, 1, tzinfo=dt.UTC))
        assert out == "4 Sep 2026 04:35 – 9 Sep 2026 06:11 UTC"

    def test_seconds_appear_only_where_they_distinguish_something(self):
        short = format_range(A, A + dt.timedelta(hours=3))
        long = format_range(A, A + dt.timedelta(days=4))
        assert short.count(":") > long.count(":")


class TestItDoesNotClaimToKnowWhereTheReaderIs:
    def test_utc_is_called_utc(self):
        assert zone_of(A) == "UTC"

    def test_and_any_other_offset_is_given_as_an_offset(self):
        # Not a zone name: "CDT" would be a claim about a place, which a
        # server rendering a figure has no basis for.
        moment = A.astimezone(dt.timezone(dt.timedelta(hours=-5)))
        assert zone_of(moment) == "−05:00"
        assert format_range(moment, moment + dt.timedelta(minutes=1)).endswith("−05:00")

    def test_a_naive_moment_claims_no_zone_at_all(self):
        naive = A.replace(tzinfo=None)
        assert zone_of(naive) == ""
        assert format_range(naive, naive + dt.timedelta(minutes=1)) == (
            "4 Sep 2026, 04:35:57–04:36:58"
        )


def test_the_ends_may_arrive_either_way_round():
    later = A + dt.timedelta(hours=2)
    assert format_range(later, A) == format_range(A, later)


@pytest.mark.parametrize(
    "span",
    [dt.timedelta(seconds=1), dt.timedelta(minutes=90), dt.timedelta(days=400)],
)
def test_it_always_returns_one_line(span):
    out = format_range(A, A + span)
    assert "\n" not in out and out.strip() == out
