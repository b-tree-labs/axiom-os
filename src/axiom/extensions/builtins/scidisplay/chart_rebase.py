# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Comparing shapes rather than magnitudes.

Two channels that move together are hard to see together when one reads 950
and the other reads 21: the small one is a flat line along the bottom, and the
figure is about the large one. Rebasing is the standard answer — every series
as its change from where it started, so they share one axis and the question
becomes "which moved, and when".

It also unlocks the comparison a third unit blocks. A figure may carry two
units and no more, because nobody holds three scales; rebased, there is only
one scale, and a temperature, a flow and a pressure can be put on it together.

## What it refuses

**A series that starts at zero cannot be rebased.** Change *from* zero is not
a percentage of anything, and dividing by it would emit an infinity or a
number the size of the first reading's rounding error. Such a series is left
out and NAMED rather than drawn as a spike.

**The reference travels with the number.** A bare percent is the failure mode
this codebase has a rule about: it passes every unit check and still means
nothing without saying "of what". So rebasing always returns the sentence that
says which instant every series is measured from, and a caller that draws the
figure without it is drawing an unlabelled ratio.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime

from .chart_svg import Series

__all__ = ["Rebased", "PERCENT_CHANGE", "rebase", "REBASE_MODES"]

#: The one mode, named rather than a bare boolean, because a second (rebase to
#: an index of 100, rebase to a chosen instant) is a real thing to want and a
#: boolean cannot grow into it.
PERCENT_CHANGE = "percent"

REBASE_MODES = (PERCENT_CHANGE,)


#: A base smaller than this share of its series' span is refused. Below it
#: the percentages run past 10,000 % and the figure is about the base's
#: rounding, not the readings: a power channel idling at 0.0004 W before a run
#: to 1 MW gave 3 × 10¹¹ %, and an axis labelled 300000000000.
MIN_BASE_SHARE = 0.01


@dataclass(frozen=True)
class Rebased:
    """The series, rebased, and the sentence that makes them readable."""

    series: tuple[Series, ...] = ()
    #: Series that could not be rebased, with why. Named, never dropped quietly.
    refused: tuple[tuple[str, str], ...] = ()
    #: What every value is now measured from. Belongs ON the figure.
    reference: str = ""
    #: The base each series was measured from, by name. A caller drawing a
    #: SWEEP hands these back on every frame, so the reference stays where it
    #: was when the sweep began rather than moving with the frame — a moving
    #: reference made every frame rescale and the axis flicker between
    #: percent and nonsense.
    bases: tuple[tuple[str, float], ...] = ()

    def __bool__(self) -> bool:
        return bool(self.series)


def _first_reading(s: Series) -> tuple[datetime, float] | None:
    for moment, value in s.points:
        if value is not None:
            return moment, value
    return None


def rebase(
    series: list[Series],
    *,
    mode: str = PERCENT_CHANGE,
    bases: dict[str, float] | None = None,
) -> Rebased:
    """Every series as its change from its own first reading in this window.

    Each series is measured from ITS OWN start, not from a shared instant: two
    channels that began recording at different times have no common first
    reading, and holding one of them to the other's would make a figure whose
    baseline is a moment one of the series was not being read at.
    """
    if mode not in REBASE_MODES:
        raise ValueError(f"unknown rebase mode {mode!r}; expected one of {REBASE_MODES}")

    out: list[Series] = []
    refused: list[tuple[str, str]] = []
    used: list[tuple[str, float]] = []
    earliest: datetime | None = None

    for s in series:
        first = _first_reading(s)
        if first is None:
            refused.append((s.name, "has no readings in this window"))
            continue
        moment, base = first
        # A base the caller fixed on an earlier frame wins over this frame's
        # first reading: that is how a sweep keeps one reference.
        if bases and s.name in bases:
            base = bases[s.name]
        if base == 0:
            refused.append((s.name, "starts at zero, and change from zero is not a ratio"))
            continue
        values = [v for _, v in s.points if v is not None]
        span = (max(values) - min(values)) if values else 0.0
        if span and abs(base) < span * MIN_BASE_SHARE:
            refused.append((
                s.name,
                f"starts at {base:g}, too near zero for a ratio against readings that reach "
                f"{max(values):g}; the figure would be about the base's rounding, not the readings",
            ))
            continue
        used.append((s.name, base))
        earliest = moment if earliest is None else min(earliest, moment)
        out.append(
            replace(
                s,
                points=[
                    (t, None if v is None else (v / base - 1.0) * 100.0)
                    for t, v in s.points
                ],
                unit="%",
            )
        )

    reference = ""
    if out:
        # The axis says 08:00, not 2026-09-10T08:00:00+00:00, and this line
        # sits under the axis. An ISO stamp here was the one thing in the
        # picture in a different notation from everything around it.
        # Day AND time, in the axis's own notation: "10 Sep 2026 07:31:00 UTC".
        # A window can span days, and a reference with only a time is then
        # ambiguous; one with an ISO stamp was the one notation in the
        # picture unlike everything around it.
        when = earliest.strftime("%-d %b %Y %H:%M:%S UTC") if earliest else "the window start"
        reference = f"% change from each series' first reading, the earliest at {when}"
    return Rebased(
        series=tuple(out), refused=tuple(refused), reference=reference, bases=tuple(used)
    )
