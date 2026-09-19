# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Track A: PULSE <-> calendar sync loop — push slot, ingest external events,
push-on-reschedule (against the FakeProvider, no DB)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from axiom.extensions.builtins.schedule.calendar import sync, sync_loop
from axiom.extensions.builtins.schedule.calendar.protocol import EventSpec
from axiom.extensions.builtins.schedule.calendar.vendors.fake import FakeCalendarProvider

T0 = datetime(2026, 6, 8, 9, 0, 0, tzinfo=UTC)
HOUR = timedelta(hours=1)
CAL = "primary"


def _status(slot_id, start, **meta):
    return {"time_slot_id": slot_id, "planned_start": start, "planned_end": None,
            "metadata": {"summary": meta.get("summary", slot_id)}}


def _events(p):
    return p.list_events(calendar_id=CAL, start=T0 - 365 * 24 * HOUR, end=T0 + 365 * 24 * HOUR)


def test_push_slot_status_upserts_idempotently():
    p = FakeCalendarProvider()
    st = _status("slot-1", T0, summary="Irradiation")
    sync_loop.push_slot_status(p, calendar_id=CAL, status=st)
    sync_loop.push_slot_status(p, calendar_id=CAL, status=st)   # idempotent
    evs = _events(p)
    assert len(evs) == 1 and evs[0].metadata[sync.PULSE_ID_KEY] == "slot-1"
    assert evs[0].summary == "Irradiation"


def test_ingest_hands_external_events_to_register_slot():
    p = FakeCalendarProvider()
    # PULSE-owned (stamped) — should be skipped by ingest:
    sync.push_time_slot(p, calendar_id=CAL, time_slot_id="ours", summary="ours", start=T0)
    # externally authored (no stamp):
    p.create_event(EventSpec(summary="walk-in", start=T0 + HOUR), calendar_id=CAL)

    ingested = []
    sync_loop.ingest(p, calendar_id=CAL, window_start=T0 - HOUR, window_end=T0 + 2 * HOUR,
                     register_slot=lambda event: ingested.append(event.summary) or "new-slot")
    assert ingested == ["walk-in"]                              # only the unstamped one


def test_reschedule_hook_pushes_the_moved_slot():
    p = FakeCalendarProvider()
    # initial calendar event for the slot:
    sync_loop.push_slot_status(p, calendar_id=CAL, status=_status("slot-1", T0, summary="x"))
    # the slot moves 3h later; PULSE emits on_reschedule -> the hook pushes the update:
    statuses = {"slot-1": _status("slot-1", T0 + 3 * HOUR, summary="x")}
    hook = sync_loop.make_reschedule_sync(p, CAL, resolve_status=lambda sid: statuses[sid])
    hook({"time_slot_id": "slot-1"})

    evs = _events(p)
    assert len(evs) == 1                                        # still one event (upsert by stamp)
    assert evs[0].start == T0 + 3 * HOUR                        # moved
