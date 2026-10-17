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


@dataclass(frozen=True)
class Rebased:
    """The series, rebased, and the sentence that makes them readable."""

    series: tuple[Series, ...] = ()
    #: Series that could not be rebased, with why. Named, never dropped quietly.
    refused: tuple[tuple[str, str], ...] = ()
    #: What every value is now measured from. Belongs ON the figure.
    reference: str = ""

    def __bool__(self) -> bool:
        return bool(self.series)


def _first_reading(s: Series) -> tuple[datetime, float] | None:
    for moment, value in s.points:
        if value is not None:
            return moment, value
    return None


def rebase(series: list[Series], *, mode: str = PERCENT_CHANGE) -> Rebased:
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
    earliest: datetime | None = None

    for s in series:
        first = _first_reading(s)
        if first is None:
            refused.append((s.name, "has no readings in this window"))
            continue
        moment, base = first
        if base == 0:
            refused.append((s.name, "starts at zero, and change from zero is not a ratio"))
            continue
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
        when = earliest.isoformat(timespec="seconds") if earliest else "the window start"
        reference = (
            f"change from each series' first reading in this window, "
            f"the earliest at {when}"
        )
    return Rebased(series=tuple(out), refused=tuple(refused), reference=reference)
