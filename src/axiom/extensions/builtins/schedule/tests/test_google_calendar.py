# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""GoogleCalendarProvider — event-resource mapping + CRUD flow, tested against
an injected fake service (no network, no creds). The live round-trip against a
real calendar is a thin confirmation of this same mapping."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from axiom.extensions.builtins.schedule import calendar
from axiom.extensions.builtins.schedule.calendar import binding
from axiom.extensions.builtins.schedule.calendar.protocol import (
    CalendarCapability,
    EventSpec,
)
from axiom.extensions.builtins.schedule.calendar.vendors import google

MON = datetime(2026, 6, 8, 9, 0, 0, tzinfo=UTC)
HOUR = timedelta(hours=1)


# --- a fake googleapiclient discovery resource (mimics the chaining) ---

class _Req:
    def __init__(self, result):
        self._r = result

    def execute(self):
        return self._r


class _Events:
    def __init__(self, store):
        self.store = store

    def list(self, *, calendarId, timeMin=None, timeMax=None, singleEvents=None, **kw):
        return _Req({"items": list(self.store.values())})

    def insert(self, *, calendarId, body):
        eid = uuid.uuid4().hex
        e = dict(body, id=eid, iCalUID=eid, etag='"1"')
        self.store[eid] = e
        return _Req(e)

    def update(self, *, calendarId, eventId, body):
        e = dict(body, id=eventId)
        self.store[eventId] = e
        return _Req(e)

    def delete(self, *, calendarId, eventId):
        self.store.pop(eventId, None)
        return _Req({})


class _CalList:
    def list(self):
        return _Req({"items": [{"id": "primary"}]})


class _FakeGoogleService:
    def __init__(self):
        self.store = {}

    def events(self):
        return _Events(self.store)

    def calendarList(self):
        return _CalList()


def test_event_to_spec_mapping():
    event = {
        "id": "abc",
        "summary": "Standup",
        "start": {"dateTime": "2026-06-08T09:00:00+00:00", "timeZone": "UTC"},
        "end": {"dateTime": "2026-06-08T09:15:00+00:00", "timeZone": "UTC"},
        "recurrence": ["RRULE:FREQ=WEEKLY;BYDAY=MO"],
        "attendees": [{"email": "a@x.com"}],
        "extendedProperties": {"private": {"slot": "s1"}},
        "iCalUID": "u",
        "etag": '"1"',
    }
    spec = google.event_to_spec(event, "primary")
    assert spec.summary == "Standup"
    assert spec.rrule == "RRULE:FREQ=WEEKLY;BYDAY=MO"
    assert spec.attendees == ["a@x.com"]
    assert spec.metadata == {"slot": "s1"}
    assert spec.ref.event_id == "abc" and spec.ref.vendor == "google"


def test_spec_to_body_mapping():
    spec = EventSpec(summary="X", start=MON, end=MON + timedelta(minutes=15),
                     rrule="RRULE:FREQ=DAILY", timezone="UTC",
                     attendees=["a@x.com"], metadata={"slot": "s1"})
    body = google.spec_to_body(spec)
    assert body["summary"] == "X"
    assert body["recurrence"] == ["RRULE:FREQ=DAILY"]
    assert body["start"]["timeZone"] == "UTC"
    assert body["attendees"] == [{"email": "a@x.com"}]
    assert body["extendedProperties"]["private"] == {"slot": "s1"}


def test_provider_crud_via_injected_service():
    svc = _FakeGoogleService()
    p = calendar.get_provider("google", {"service": svc, "calendar_id": "primary"})
    assert p.health() is True

    ref = p.create_event(EventSpec(summary="Sync", start=MON,
                                   rrule="RRULE:FREQ=WEEKLY;BYDAY=WE"))
    assert ref.vendor == "google"

    events = p.list_events(start=MON - HOUR, end=MON + HOUR)
    assert len(events) == 1
    assert events[0].summary == "Sync"
    assert events[0].rrule == "RRULE:FREQ=WEEKLY;BYDAY=WE"
    # the imported event binds to an rrule cadence
    assert binding.event_to_cadence(events[0]).kind == "rrule"

    p.update_event(ref, EventSpec(summary="Sync v2", start=MON))
    assert p.list_events(start=MON - HOUR, end=MON + HOUR)[0].summary == "Sync v2"

    p.delete_event(ref)
    assert p.list_events(start=MON - HOUR, end=MON + HOUR) == []


def test_capabilities_are_honest():
    p = calendar.get_provider("google", {"service": _FakeGoogleService()})
    assert CalendarCapability.CREATE_EVENT in p.capabilities
    assert CalendarCapability.WATCH not in p.capabilities  # sync loop lands later
