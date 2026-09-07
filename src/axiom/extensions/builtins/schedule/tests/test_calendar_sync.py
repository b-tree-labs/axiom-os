# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Two-way calendar sync — the 'beyond initial read' matrix, against the
FakeProvider: idempotent reconcile (a booking that recurs for years),
reschedule propagation (a slot that moves), cancel cascade, recurrence, and
externally-authored ingest."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from axiom.extensions.builtins.schedule.calendar import sync
from axiom.extensions.builtins.schedule.calendar.protocol import EventSpec
from axiom.extensions.builtins.schedule.calendar.vendors.fake import FakeCalendarProvider

MON = datetime(2026, 6, 8, 9, 0, 0, tzinfo=UTC)
HOUR = timedelta(hours=1)
CAL = "primary"


def _events(p):
    return p.list_events(calendar_id=CAL, start=MON - 365 * 24 * HOUR, end=MON + 365 * 24 * HOUR)


def test_push_creates_a_stamped_event():
    p = FakeCalendarProvider()
    sync.push_time_slot(p, calendar_id=CAL, time_slot_id="slot-1",
                        summary="Reserved slot", start=MON, end=MON + HOUR)
    evs = _events(p)
    assert len(evs) == 1
    assert evs[0].metadata[sync.PULSE_ID_KEY] == "slot-1"


def test_resync_is_idempotent_and_updates_in_place():
    p = FakeCalendarProvider()
    sync.push_time_slot(p, calendar_id=CAL, time_slot_id="slot-1", summary="v1", start=MON)
    sync.push_time_slot(p, calendar_id=CAL, time_slot_id="slot-1", summary="v2", start=MON)
    evs = _events(p)
    assert len(evs) == 1                       # no duplicate (a daily booking, re-synced for years)
    assert evs[0].summary == "v2"              # updated in place


def test_reschedule_propagates_by_stamp_not_time():
    p = FakeCalendarProvider()
    sync.push_time_slot(p, calendar_id=CAL, time_slot_id="slot-1", summary="x", start=MON)
    # The slot is moved 3h later. Same stamp -> same event, new time (no orphan).
    sync.push_time_slot(p, calendar_id=CAL, time_slot_id="slot-1", summary="x", start=MON + 3 * HOUR)
    evs = _events(p)
    assert len(evs) == 1
    assert evs[0].start == MON + 3 * HOUR


def test_delete_for_time_slot_cancels_the_event():
    p = FakeCalendarProvider()
    sync.push_time_slot(p, calendar_id=CAL, time_slot_id="slot-1", summary="x", start=MON)
    assert sync.delete_for_time_slot(p, calendar_id=CAL, time_slot_id="slot-1") is True
    assert _events(p) == []
    assert sync.delete_for_time_slot(p, calendar_id=CAL, time_slot_id="slot-1") is False  # already gone


def test_recurring_slot_round_trips_to_an_rrule_cadence():
    p = FakeCalendarProvider()
    sync.push_time_slot(p, calendar_id=CAL, time_slot_id="recurring-1", summary="Daily booking",
                        start=MON, rrule="RRULE:FREQ=DAILY")
    pulled = sync.pull_events(p, calendar_id=CAL, window_start=MON - HOUR, window_end=MON + HOUR)
    assert len(pulled) == 1
    assert pulled[0]["pulse_id"] == "recurring-1"
    assert pulled[0]["cadence"].kind == "rrule"


def test_pull_distinguishes_externally_authored_events():
    p = FakeCalendarProvider()
    # PULSE-owned (stamped):
    sync.push_time_slot(p, calendar_id=CAL, time_slot_id="slot-1", summary="ours", start=MON)
    # externally-authored (no stamp) — someone booked directly on the calendar:
    p.create_event(EventSpec(summary="external", start=MON + 2 * HOUR), calendar_id=CAL)
    pulled = sync.pull_events(p, calendar_id=CAL, window_start=MON - HOUR, window_end=MON + 3 * HOUR)
    by_summary = {e["event"].summary: e for e in pulled}
    assert by_summary["ours"]["pulse_id"] == "slot-1"
    assert by_summary["external"]["pulse_id"] is None       # candidate to ingest as a new slot
