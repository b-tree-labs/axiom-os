# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Track F: CalDAV provider — iCalendar VEVENT round-trip (RRULE maps directly)
+ CRUD over a fake CalDAV client (no network)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from axiom.extensions.builtins.schedule import calendar
from axiom.extensions.builtins.schedule.calendar.protocol import EventSpec
from axiom.extensions.builtins.schedule.calendar.vendors import caldav

T0 = datetime(2026, 6, 8, 9, 0, 0, tzinfo=UTC)
HOUR = timedelta(hours=1)


class _FakeCalDAV:
    def __init__(self):
        self.store = {}

    def put(self, href, ical):
        self.store[href] = ical

    def delete(self, href):
        self.store.pop(href, None)

    def query(self, calendar_id, start, end):
        return [{"href": h, "ical": i} for h, i in self.store.items()]


def _provider():
    return calendar.get_provider("caldav", {"client": _FakeCalDAV(), "calendar_id": "home"})


def test_ical_round_trip_preserves_rrule_attendees_and_stamp():
    spec = EventSpec(summary="Sync", start=T0, end=T0 + HOUR,
                     rrule="RRULE:FREQ=WEEKLY;BYDAY=MO,WE,FR",
                     attendees=["op@x", "@AXI:axiom"],
                     metadata={"pulse_slot_id": "slot-1"})
    back = caldav.ical_to_spec(caldav.spec_to_ical(spec), "home")
    assert back.summary == "Sync"
    assert back.rrule == "RRULE:FREQ=WEEKLY;BYDAY=MO,WE,FR"     # iCal RRULE maps directly
    assert back.attendees == ["op@x", "@AXI:axiom"]
    assert back.metadata["pulse_slot_id"] == "slot-1"


def test_provider_crud_and_find_by_stamp():
    p = _provider()
    assert p.health() is True
    ref = p.create_event(EventSpec(summary="Reserved", start=T0,
                                   rrule="RRULE:FREQ=DAILY",
                                   metadata={"pulse_slot_id": "slot-9"}))
    assert ref.vendor == "caldav"

    found = p.find_event(private_key="pulse_slot_id", private_value="slot-9")
    assert found is not None and found.summary == "Reserved"
    assert found.rrule == "RRULE:FREQ=DAILY"

    p.update_event(ref, EventSpec(summary="Reserved v2", start=T0))
    assert p.list_events()[0].summary == "Reserved v2"

    p.delete_event(ref)
    assert p.find_event(private_key="pulse_slot_id", private_value="slot-9") is None
