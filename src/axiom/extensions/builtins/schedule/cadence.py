# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Cadence → next_fire_at computation.

Per spec-axiom-schedule §3 + §6: cron uses ``croniter``; interval uses
plain arithmetic; jitter is uniform(0, jitter_seconds). The output
is always a UTC ``datetime`` written to ``schedule_definition.next_fire_at``.

PULSE-1 supports interval + cron + one_shot. Trigger raises (per §7).
"""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta
from typing import Optional

from axiom.extensions.builtins.schedule.api import Cadence


def compute_next_fire_at(
    cadence: Cadence,
    last_fire: Optional[datetime],
    now: datetime,
    *,
    rng: Optional[random.Random] = None,
) -> Optional[datetime]:
    """Return the next scheduled fire time, or ``None`` if cadence is exhausted.

    Args:
        cadence: the cadence spec
        last_fire: the previous fire time, or None for the first computation
        now: the current UTC time, used as the anchor when ``last_fire`` is None
        rng: optional ``random.Random`` for deterministic jitter in tests
    """
    if cadence.kind == "trigger":
        # Trigger schedules sit with next_fire_at = NULL until the matcher
        # loop writes now() in PULSE-2.
        return None

    anchor = last_fire if last_fire is not None else now

    if cadence.kind == "one_shot":
        if last_fire is not None:
            return None  # already fired
        if cadence.not_before is not None:
            return cadence.not_before
        return now

    if cadence.kind == "interval":
        if cadence.interval is None:
            raise ValueError("interval cadence requires interval=...")
        nxt = anchor + cadence.interval

    elif cadence.kind == "cron":
        if cadence.cron is None:
            raise ValueError("cron cadence requires cron=...")
        nxt = _croniter_next(cadence.cron, anchor, cadence.tz)

    elif cadence.kind == "rrule":
        if cadence.rrule is None:
            raise ValueError("rrule cadence requires rrule=...")
        # First computation includes the boundary (DTSTART is itself an
        # occurrence); advancing past a fire is strictly-after.
        nxt = _rrule_next(
            cadence.rrule, cadence.not_before, anchor, inclusive=last_fire is None
        )
        if nxt is None:
            return None  # recurrence exhausted (COUNT / UNTIL)

    else:
        raise ValueError(f"unknown cadence kind: {cadence.kind!r}")

    if cadence.randomized_delay is not None and cadence.randomized_delay.total_seconds() > 0:
        jitter = (rng or random).uniform(0, cadence.randomized_delay.total_seconds())
        nxt = nxt + timedelta(seconds=jitter)

    if cadence.not_after is not None and nxt > cadence.not_after:
        return None
    return nxt


def _croniter_next(expr: str, anchor: datetime, tz: str) -> datetime:
    """Compute the next cron fire after ``anchor`` in timezone ``tz``."""
    try:
        from croniter import croniter
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "croniter is required for cron cadences; pin it in pyproject."
        ) from exc

    # croniter accepts naive datetimes in the configured tz.
    # Anchor must be timezone-aware; we hand it through verbatim and rely
    # on croniter's tz handling. UTC is the platform default.
    base = anchor if anchor.tzinfo is not None else anchor.replace(tzinfo=UTC)
    it = croniter(expr, base)
    nxt: datetime = it.get_next(datetime)
    if nxt.tzinfo is None:
        nxt = nxt.replace(tzinfo=UTC)
    return nxt


def _rrule_next(
    rule: str, dtstart: Optional[datetime], anchor: datetime, *, inclusive: bool = False
) -> Optional[datetime]:
    """Next occurrence of an iCalendar RRULE after ``anchor`` (inclusive of the
    boundary when ``inclusive``).

    ``dtstart`` anchors the recurrence (required for COUNT/UNTIL correctness);
    when absent, ``anchor`` is used. Returns None if the recurrence is exhausted.
    """
    from dateutil.rrule import rrulestr

    ds = dtstart if dtstart is not None else anchor
    if ds.tzinfo is None:
        ds = ds.replace(tzinfo=UTC)
    spec = rule if rule.upper().startswith(("RRULE", "DTSTART", "FREQ")) else f"RRULE:{rule}"
    robj = rrulestr(spec, dtstart=ds)
    a = anchor if anchor.tzinfo is not None else anchor.replace(tzinfo=UTC)
    nxt = robj.after(a, inc=inclusive)
    if nxt is not None and nxt.tzinfo is None:
        nxt = nxt.replace(tzinfo=UTC)
    return nxt


__all__ = ["compute_next_fire_at"]
