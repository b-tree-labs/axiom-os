# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Bind calendar events to PULSE cadences and back.

A recurring calendar event carries an iCalendar RRULE → an ``rrule`` cadence
anchored at the event start (lossless). A one-off event → a ``one_shot``. Going
the other way, any cadence PULSE can express as an RRULE (rrule directly, or an
interval) becomes a recurring event; the rest export as a single event at the
next fire. This is the seam the calendar connector syncs across.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from axiom.extensions.builtins.schedule import formats
from axiom.extensions.builtins.schedule.api import Cadence
from axiom.extensions.builtins.schedule.calendar.protocol import EventSpec


def event_to_cadence(spec: EventSpec) -> Cadence:
    """A calendar event → the cadence that reproduces its firing."""
    if spec.rrule:
        return Cadence(
            kind="rrule", rrule=spec.rrule, not_before=spec.start, tz=spec.timezone
        )
    return Cadence(kind="one_shot", not_before=spec.start, tz=spec.timezone)


def cadence_to_event_spec(
    cadence: Cadence,
    *,
    summary: str,
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
    metadata: Optional[dict] = None,
) -> EventSpec:
    """A cadence → a calendar event spec. Recurring cadences (rrule/interval)
    export an RRULE; one_shot exports a single event at its start."""
    spec = EventSpec(
        summary=summary,
        start=start or cadence.not_before or datetime.now(tz=None),
        end=end,
        timezone=cadence.tz,
        metadata=metadata or {},
    )
    if cadence.kind in ("rrule", "interval", "cron"):
        # to_rrule yields "RRULE:..."; calendars want the bare rule line list,
        # but we keep the prefix-normalized string and strip on emit per vendor.
        spec.rrule = formats.to_rrule(cadence)
    return spec


__all__ = ["cadence_to_event_spec", "event_to_cadence"]
