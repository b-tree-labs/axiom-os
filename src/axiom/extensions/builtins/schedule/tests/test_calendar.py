# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Calendar factory + provider Protocol + event⇄cadence binding, exercised
through the in-memory FakeCalendarProvider (the deterministic test vehicle).
The live Google / M365 adapters conform to this same Protocol."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from axiom.extensions.builtins.schedule import calendar
from axiom.extensions.builtins.schedule.api import Cadence
from axiom.extensions.builtins.schedule.cadence import compute_next_fire_at
from axiom.extensions.builtins.schedule.calendar import binding
from axiom.extensions.builtins.schedule.calendar.protocol import (
    CalendarCapability,
    CapabilityUnsupported,
    EventSpec,
    require,
)

MON = datetime(2026, 6, 8, 9, 0, 0, tzinfo=UTC)
HOUR = timedelta(hours=1)


def _n(dt):
    return dt.replace(tzinfo=None) if dt is not None and dt.tzinfo else dt


def test_factory_lists_and_builds_vendors():
    assert "fake" in calendar.available_vendors()
    assert calendar.get_provider("fake").vendor == "fake"
    with pytest.raises(KeyError):
        calendar.get_provider("nope")


def test_detect_reports_connector_state():
    assert calendar.detect("fake")["state"] == "configured"
    assert calendar.detect("fake", {"healthy": False})["state"] == "broken"
    assert calendar.detect("nope")["state"] == "absent"


def test_fake_provider_crud_round_trip():
    p = calendar.get_provider("fake")
    ref = p.create_event(EventSpec(summary="Standup", start=MON,
                                   rrule="RRULE:FREQ=WEEKLY;BYDAY=MO"))
    events = p.list_events(start=MON - HOUR, end=MON + HOUR)
    assert len(events) == 1 and events[0].summary == "Standup"

    p.update_event(ref, EventSpec(summary="Standup (moved)", start=MON + HOUR))
    assert p.list_events(start=MON, end=MON + 2 * HOUR)[0].summary == "Standup (moved)"

    p.delete_event(ref)
    assert p.list_events(start=MON - HOUR, end=MON + 2 * HOUR) == []


def test_event_to_cadence_rrule_and_oneshot():
    cad = binding.event_to_cadence(EventSpec(summary="x", start=MON, rrule="RRULE:FREQ=DAILY"))
    assert cad.kind == "rrule"
    assert _n(compute_next_fire_at(cad, None, MON)) == _n(MON)  # daily, first incl.

    one = binding.event_to_cadence(EventSpec(summary="y", start=MON))
    assert one.kind == "one_shot" and _n(one.not_before) == _n(MON)


def test_cadence_to_event_spec_exports_rrule():
    spec = binding.cadence_to_event_spec(
        Cadence(kind="interval", interval=HOUR), summary="Hourly", start=MON
    )
    assert spec.rrule == "RRULE:FREQ=HOURLY"

    rcad = Cadence(kind="rrule", rrule="RRULE:FREQ=WEEKLY;BYDAY=FR", not_before=MON)
    assert binding.cadence_to_event_spec(rcad, summary="Fri").rrule == "RRULE:FREQ=WEEKLY;BYDAY=FR"


def test_round_trip_event_to_cadence_to_event():
    original = EventSpec(summary="Weekly sync", start=MON, rrule="RRULE:FREQ=WEEKLY;BYDAY=MO,WE")
    cad = binding.event_to_cadence(original)
    back = binding.cadence_to_event_spec(cad, summary=original.summary, start=original.start)
    assert back.rrule == original.rrule


def test_capability_matrix_is_enforced():
    require(calendar.get_provider("fake"), CalendarCapability.CREATE_EVENT)  # ok

    class _NoCaps:
        vendor = "empty"
        capabilities = frozenset()

    with pytest.raises(CapabilityUnsupported):
        require(_NoCaps(), CalendarCapability.CREATE_EVENT)
