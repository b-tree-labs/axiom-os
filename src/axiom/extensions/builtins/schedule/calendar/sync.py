# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Two-way calendar sync — keep a calendar and PULSE in agreement.

Every event PULSE owns is stamped with the PULSE time-slot id in
``extendedProperties.private`` (``pulse_slot_id``). That stamp is the reconcile
key: sync **upserts by stamp**, so it finds its own writes regardless of time
changes and never double-creates. The three primitives:

- ``push_time_slot`` — create-or-update the calendar event for a PULSE slot
  (idempotent; survives reschedules because matching is by stamp, not time).
- ``delete_for_time_slot`` — remove the slot's event (the cancel-cascade half).
- ``pull_events`` — read events in a window and bind each to a cadence, tagging
  which are PULSE-owned (stamped) vs operator-authored (to be ingested).

Truth-ownership conflict resolution (both sides edited) and RSVP/WATCH ingest
are the next increments; see the test matrix in test_calendar_sync.py.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from axiom.extensions.builtins.schedule.calendar import binding
from axiom.extensions.builtins.schedule.calendar.protocol import EventRef, EventSpec

PULSE_ID_KEY = "pulse_slot_id"


def push_time_slot(
    provider: Any,
    *,
    calendar_id: str,
    time_slot_id: str,
    summary: str,
    start: datetime,
    end: Optional[datetime] = None,
    rrule: Optional[str] = None,
    attendees: Optional[list] = None,
    timezone: str = "UTC",
) -> EventRef:
    """Upsert the calendar event mirroring a PULSE slot, matched by stamp."""
    existing = provider.find_event(
        calendar_id=calendar_id, private_key=PULSE_ID_KEY, private_value=time_slot_id
    )
    spec = EventSpec(
        summary=summary, start=start, end=end, rrule=rrule, timezone=timezone,
        attendees=attendees or [], metadata={PULSE_ID_KEY: time_slot_id},
    )
    if existing is not None and existing.ref is not None:
        return provider.update_event(existing.ref, spec)
    return provider.create_event(spec, calendar_id=calendar_id)


def delete_for_time_slot(provider: Any, *, calendar_id: str, time_slot_id: str) -> bool:
    """Delete the slot's event. Returns True if one was removed."""
    existing = provider.find_event(
        calendar_id=calendar_id, private_key=PULSE_ID_KEY, private_value=time_slot_id
    )
    if existing is None or existing.ref is None:
        return False
    provider.delete_event(existing.ref)
    return True


def pull_events(
    provider: Any, *, calendar_id: str, window_start: datetime, window_end: datetime
) -> list[dict]:
    """Read events in a window, each bound to a cadence and tagged by origin.

    ``pulse_id`` is set for PULSE-owned events (already mirrored); events without
    it are operator-authored and candidates to ingest as new slots.
    """
    out = []
    for event in provider.list_events(
        calendar_id=calendar_id, start=window_start, end=window_end
    ):
        out.append({
            "event": event,
            "pulse_id": event.metadata.get(PULSE_ID_KEY),
            "cadence": binding.event_to_cadence(event),
        })
    return out


__all__ = ["PULSE_ID_KEY", "delete_for_time_slot", "pull_events", "push_time_slot"]
