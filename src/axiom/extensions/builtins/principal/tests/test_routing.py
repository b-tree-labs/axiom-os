# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Routing is deterministic, inspectable, and never silently drops a message.

The rules under test are the ones an incident review will ask about: an
unverified endpoint is never chosen, a failed one is skipped, quiet hours defer
rather than discard, and every decision explains itself.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from axiom.extensions.builtins.principal.models import (
    ChannelPreference,
    ContactEndpoint,
    EndpointHealth,
    PrincipalProfile,
)
from axiom.extensions.builtins.principal.routing import Decision, route

VERIFIED = "2026-08-19T00:00:00+00:00"


def _principal(**overrides):
    endpoints = overrides.pop(
        "endpoints",
        [
            ContactEndpoint(kind="chat", address="room:ops", verified_at=VERIFIED),
            ContactEndpoint(kind="email", address="p@example.test", verified_at=VERIFIED),
            ContactEndpoint(kind="inbox", address="local", verified_at=VERIFIED),
        ],
    )
    prefs = overrides.pop(
        "preferences",
        [
            ChannelPreference(
                topic_class="incident", ranked_kinds=["chat", "email"], urgency_floor=3
            ),
            ChannelPreference(topic_class="*", ranked_kinds=["email", "inbox"], urgency_floor=5),
        ],
    )
    return PrincipalProfile(
        handle="@ben:netl",
        display_name="Test Principal",
        endpoints=endpoints,
        preferences=prefs,
        quiet_hours=overrides.pop("quiet_hours", None),
        **overrides,
    )


def test_first_ranked_verified_channel_wins():
    d = route(_principal(), topic="incident", urgency=5, now=datetime(2026, 8, 19, 12, 0))
    assert isinstance(d, Decision)
    assert d.endpoint.kind == "chat"
    assert d.deferred_until is None


def test_unverified_endpoint_is_never_selected():
    p = _principal(
        endpoints=[
            ContactEndpoint(kind="chat", address="room:ops", verified_at=None),
            ContactEndpoint(kind="email", address="p@example.test", verified_at=VERIFIED),
        ]
    )
    d = route(p, topic="incident", urgency=5, now=datetime(2026, 8, 19, 12, 0))
    assert d.endpoint.kind == "email"
    assert any("unverified" in r.lower() for r in d.rejected_reasons.values())


def test_failed_endpoint_is_skipped_but_degraded_is_still_usable():
    p = _principal(
        endpoints=[
            ContactEndpoint(
                kind="chat", address="room:ops", verified_at=VERIFIED, health=EndpointHealth.FAILED
            ),
            ContactEndpoint(
                kind="email",
                address="p@example.test",
                verified_at=VERIFIED,
                health=EndpointHealth.DEGRADED,
            ),
        ]
    )
    d = route(p, topic="incident", urgency=5, now=datetime(2026, 8, 19, 12, 0))
    assert d.endpoint.kind == "email", "degraded is a warning, not a disqualification"


def test_quiet_hours_defer_low_urgency_rather_than_drop():
    p = _principal(quiet_hours=("22:00", "07:00"))
    d = route(p, topic="incident", urgency=1, now=datetime(2026, 8, 19, 23, 30))
    assert d.deferred_until is not None
    assert d.endpoint is not None, "a deferred message still has a destination"


def test_urgent_message_breaks_quiet_hours():
    p = _principal(quiet_hours=("22:00", "07:00"))
    d = route(p, topic="incident", urgency=9, now=datetime(2026, 8, 19, 23, 30))
    assert d.deferred_until is None


def test_unknown_topic_falls_back_to_wildcard():
    d = route(_principal(), topic="never-seen", urgency=9, now=datetime(2026, 8, 19, 12, 0))
    assert d.endpoint.kind == "email"


def test_no_usable_channel_falls_back_to_inbox_and_flags_unreachable():
    p = _principal(
        endpoints=[ContactEndpoint(kind="inbox", address="local", verified_at=VERIFIED)],
        preferences=[ChannelPreference(topic_class="*", ranked_kinds=["chat", "email"])],
    )
    d = route(p, topic="anything", urgency=9, now=datetime(2026, 8, 19, 12, 0))
    assert d.endpoint.kind == "inbox"
    assert d.unreachable is True, "silence is never an outcome; the caller must know"


def test_every_decision_explains_itself():
    d = route(_principal(), topic="incident", urgency=5, now=datetime(2026, 8, 19, 12, 0))
    assert d.explain, "a routing choice you cannot interrogate cannot be trusted"
    assert "chat" in d.explain


def test_escalation_order_is_the_ranked_remainder():
    d = route(_principal(), topic="incident", urgency=5, now=datetime(2026, 8, 19, 12, 0))
    assert [e.kind for e in d.escalation] == ["email"]


@pytest.mark.parametrize(
    "now,expected_deferred",
    [
        (datetime(2026, 8, 19, 21, 59), False),
        (datetime(2026, 8, 19, 22, 0), True),
        (datetime(2026, 8, 20, 6, 59), True),
        (datetime(2026, 8, 20, 7, 0), False),
    ],
)
def test_quiet_hours_boundaries_over_midnight(now, expected_deferred):
    p = _principal(quiet_hours=("22:00", "07:00"))
    d = route(p, topic="incident", urgency=1, now=now)
    assert (d.deferred_until is not None) is expected_deferred
