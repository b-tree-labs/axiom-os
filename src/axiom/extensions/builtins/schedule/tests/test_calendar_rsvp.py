# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Track B: RSVP capture + the operator-veto pre_fire gate (a required attendee
— person or agent — must accept before the event fires)."""

from __future__ import annotations

from datetime import UTC, datetime

from axiom.extensions.builtins.schedule.calendar import rsvp
from axiom.extensions.builtins.schedule.calendar.protocol import EventSpec
from axiom.extensions.builtins.schedule.calendar.vendors import google, m365

T0 = datetime(2026, 6, 8, 9, 0, 0, tzinfo=UTC)


def test_google_and_m365_map_rsvp_status():
    g = google.event_to_spec({
        "id": "e", "start": {"dateTime": "2026-06-08T09:00:00+00:00"},
        "attendees": [{"email": "op@x", "responseStatus": "accepted"},
                      {"email": "pi@x", "responseStatus": "needsAction"}],
    }, "primary")
    assert g.rsvps == {"op@x": "accepted", "pi@x": "needsAction"}

    m = m365.event_to_spec({
        "id": "e", "start": {"dateTime": "2026-06-08T09:00:00"},
        "attendees": [{"emailAddress": {"address": "op@x"}, "status": {"response": "accepted"}}],
    }, "cal")
    assert m.rsvps == {"op@x": "accepted"}


def test_operator_veto_helpers():
    spec = EventSpec(summary="x", start=T0,
                     rsvps={"operator@x": "accepted", "pi@x": "needsAction"})
    assert rsvp.response_of(spec, "operator@x") == "accepted"
    assert rsvp.all_accepted(spec, ["operator@x"]) is True
    assert rsvp.all_accepted(spec, ["operator@x", "pi@x"]) is False
    assert rsvp.awaiting(spec, ["operator@x", "pi@x"]) == ["pi@x"]


def test_pre_fire_gate_vetoes_until_required_attendee_accepts():
    # The operator (or agent @AXI) must accept before the slot fires.
    accepted = EventSpec(summary="x", start=T0, rsvps={"@AXI:axiom": "accepted"})
    pending = EventSpec(summary="x", start=T0, rsvps={"@AXI:axiom": "needsAction"})

    gate = rsvp.make_pre_fire_gate(lambda _payload: accepted, required=["@AXI:axiom"])
    assert gate({}) is True                                  # accepted -> proceed

    gate2 = rsvp.make_pre_fire_gate(lambda _payload: pending, required=["@AXI:axiom"])
    assert gate2({}) == "skip"                               # not accepted -> veto

    gate3 = rsvp.make_pre_fire_gate(lambda _payload: None, required=["@AXI:axiom"])
    assert gate3({}) == "skip"                               # unresolvable -> fail-closed
