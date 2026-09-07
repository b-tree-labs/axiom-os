# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""RSVP → operator-veto (track B).

Inviting a principal (a person *or an agent*, e.g. @AXI) to a time-anchored event
is assigning it. The invitee's RSVP is the acknowledgement: an event's fire can
be gated on a required attendee having **accepted** — the operator-veto, now
expressed through the calendar. ``make_pre_fire_gate`` produces a PULSE
``pre_fire`` hook from a resolver that fetches the linked event's RSVPs.
"""

from __future__ import annotations

from typing import Any, Callable

ACCEPTED = "accepted"


def response_of(spec: Any, attendee: str) -> str:
    """The attendee's response (``needsAction`` if not present)."""
    return spec.rsvps.get(attendee, "needsAction")


def all_accepted(spec: Any, required: list) -> bool:
    """Have all required attendees accepted?"""
    return all(spec.rsvps.get(e) == ACCEPTED for e in required)


def awaiting(spec: Any, required: list) -> list:
    """Required attendees who haven't accepted yet."""
    return [e for e in required if spec.rsvps.get(e) != ACCEPTED]


def make_pre_fire_gate(
    rsvps_resolver: Callable[[dict], Any], required: list
) -> Callable[[dict], Any]:
    """A PULSE ``pre_fire`` hook: veto (``"skip"``) until every required
    attendee (operator/agent) has accepted the linked event. Fail-closed — if the
    event can't be resolved, veto."""
    def gate(payload: dict) -> Any:
        try:
            spec = rsvps_resolver(payload)
        except Exception:  # noqa: BLE001 — can't confirm acceptance -> veto
            return "skip"
        return True if (spec is not None and all_accepted(spec, required)) else "skip"
    return gate


__all__ = ["ACCEPTED", "all_accepted", "awaiting", "make_pre_fire_gate", "response_of"]
