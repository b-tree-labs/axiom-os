# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0
"""A span of time, written for a person to read.

The provenance line under a figure carried its window as a pair of ISO
timestamps::

    2026-09-04T04:35:57.936000+00:00–2026-09-04T04:37:36.588000+00:00

Sixty characters, of which the reader needs about twenty. The date is in
there twice, the year three times counting the axis, the offset twice, and
the microseconds are a bucket boundary's rounding error presented as
measured precision — the figure is drawn from 500 ms buckets, so the .936
is an artefact of where a bucket happened to start.

ISO is the right way to WRITE a moment down and the wrong way to show one.
It is unambiguous under machine parsing, which is not the problem a line of
prose beneath a chart is solving.

What a person needs from a window is: which day, what time, how long. So
the day is said once when both ends share it, the year once, the offset
once, and the resolution follows the span — a window measured in minutes
shows seconds, one measured in days does not, because a second that does
not distinguish anything is a digit the eye has to step over.
"""

from __future__ import annotations

from datetime import datetime, timedelta

#: Under this, the ends differ by seconds and the seconds are worth showing.
SECONDS_BELOW = timedelta(days=1)

#: And under this, even the date is noise — both ends are the same minute.
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _day(moment: datetime) -> str:
    return f"{moment.day} {_MONTHS[moment.month - 1]} {moment.year}"


def _clock(moment: datetime, *, seconds: bool) -> str:
    return moment.strftime("%H:%M:%S" if seconds else "%H:%M")


def zone_of(moment: datetime) -> str:
    """What to call the offset, once, at the end.

    ``UTC`` when it is UTC, because that is the name people use for it, and
    ``+02:00`` otherwise — a named local zone would be a claim about where
    the reader is, which a server rendering a figure cannot make.
    """
    offset = moment.utcoffset()
    if offset is None:
        return ""
    if offset == timedelta(0):
        return "UTC"
    total = int(offset.total_seconds())
    sign = "+" if total >= 0 else "−"
    total = abs(total)
    return f"{sign}{total // 3600:02d}:{total % 3600 // 60:02d}"


def format_range(start: datetime, end: datetime) -> str:
    """``start``–``end`` as a line of prose.

    >>> import datetime as dt
    >>> a = dt.datetime(2026, 9, 4, 4, 35, 57, 936000, tzinfo=dt.UTC)
    >>> b = dt.datetime(2026, 9, 4, 4, 37, 36, 588000, tzinfo=dt.UTC)
    >>> format_range(a, b)
    '4 Sep 2026, 04:35:57–04:37:37 UTC'

    Across a day boundary each end carries its own date:

    >>> c = dt.datetime(2026, 9, 6, 6, 10, tzinfo=dt.UTC)
    >>> format_range(a, c)
    '4 Sep 2026 04:35 – 6 Sep 2026 06:10 UTC'
    """
    if end < start:
        start, end = end, start
    seconds = (end - start) < SECONDS_BELOW
    # Outwards, never to the nearest. A window shown narrower than it is
    # appears to exclude a reading it drew, and somebody comparing the line
    # to the label would be right to think the figure was wrong. So the
    # start floors and the end ceils, at whatever resolution is shown.
    step = timedelta(seconds=1) if seconds else timedelta(minutes=1)
    start = _floor(start, step)
    end = _ceil(end, step)
    zone = zone_of(start)
    tail = f" {zone}" if zone else ""

    if start.date() == end.date():
        # The day, once. Said at both ends it is the same seven characters
        # twice, and the eye has to compare them to find out they match.
        return (
            f"{_day(start)}, {_clock(start, seconds=seconds)}"
            f"–{_clock(end, seconds=seconds)}{tail}"
        )
    return (
        f"{_day(start)} {_clock(start, seconds=seconds)} – "
        f"{_day(end)} {_clock(end, seconds=seconds)}{tail}"
    )


def _floor(moment: datetime, step: timedelta) -> datetime:
    over = (
        moment.microsecond / 1e6
        + moment.second % max(int(step.total_seconds()), 1)
    )
    return moment - timedelta(seconds=over)


def _ceil(moment: datetime, step: timedelta) -> datetime:
    down = _floor(moment, step)
    return down if down == moment else down + step
