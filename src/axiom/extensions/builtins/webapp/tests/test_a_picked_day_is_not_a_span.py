# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Picking a day on the Charts page sends ``last=activity:<day>``; it must open that day.

The page's day menu sends the opening instruction ``activity:2026-09-29`` rather than the
day's two instants. The figure route parsed every ``last`` as a span (24h, 7d, 30m) and
answered ``not a span: 'activity:2026-04-08'``, so no day could be opened from the menu.
"""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException

from axiom.extensions.builtins.webapp.api.catalog import _opened_on, _span


def test_a_day_opens_as_that_utc_day():
    start, end = _opened_on("activity:2026-09-29")
    assert start == datetime(2026, 9, 29, tzinfo=UTC)
    assert end - start == timedelta(days=1)


def test_a_day_is_not_a_span_and_a_span_is_not_a_day():
    assert _span("activity:2026-09-29") is None
    assert _opened_on("7d") is None
    assert _span("7d") == timedelta(days=7)


def test_plain_activity_leaves_the_lane_to_choose():
    assert _opened_on("activity") is None
    assert _span("activity") is None


def test_a_malformed_day_is_refused_in_words():
    with pytest.raises(HTTPException) as err:
        _opened_on("activity:2026-13-45")
    assert err.value.status_code == 422 and "not a day" in err.value.detail


def test_a_range_opens_every_day_in_it_the_last_included():
    start, end = _opened_on("range:2026-09-24..2026-09-30")
    assert start == datetime(2026, 9, 24, tzinfo=UTC)
    assert end == datetime(2026, 10, 1, tzinfo=UTC)
    assert _span("range:2026-09-24..2026-09-30") is None


def test_a_range_given_backwards_is_the_same_range():
    assert _opened_on("range:2026-09-30..2026-09-24") == _opened_on("range:2026-09-24..2026-09-30")


def test_the_data_window_of_the_local_clone_opens():
    # 2026-09-09..2026-10-09 is the clone's window; its replay day is 09-29.
    start, end = _opened_on("range:2026-09-09..2026-10-09")
    assert (end - start).days == 31
    assert _opened_on("activity:2026-09-29")[0].isoformat().startswith("2026-09-29")


def test_a_range_without_two_days_is_refused_in_words():
    with pytest.raises(HTTPException) as err:
        _opened_on("range:2026-09-24")
    assert "not a range" in err.value.detail


def test_a_bad_span_still_says_so():
    with pytest.raises(HTTPException):
        _span("soon")
