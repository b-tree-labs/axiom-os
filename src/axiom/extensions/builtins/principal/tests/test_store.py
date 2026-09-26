# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The principal record survives a restart, including what was proven."""

from __future__ import annotations

from axiom.extensions.builtins.principal.models import (
    ChannelPreference,
    ContactEndpoint,
    EndpointHealth,
    PrincipalProfile,
)
from axiom.extensions.builtins.principal.store import load, save


def _principal():
    ep = ContactEndpoint(kind="email", address="p@example.test")
    ep.mark_verified("rcpt-1")
    return PrincipalProfile(
        handle="@ben:netl",
        display_name="Test Principal",
        endpoints=[ep, ContactEndpoint(kind="chat", address="room:ops")],
        preferences=[ChannelPreference(topic_class="*", ranked_kinds=["email"], urgency_floor=4)],
        quiet_hours=("22:00", "07:00"),
    )


def test_round_trip_preserves_verification(tmp_path):
    save(_principal(), config_dir=tmp_path)
    back = load(config_dir=tmp_path)
    email = back.endpoint("email")
    assert email.verified_at is not None, "proof of delivery must survive a restart"
    assert email.health is EndpointHealth.OK
    assert email.last_receipt_id == "rcpt-1"


def test_round_trip_preserves_preferences_and_quiet_hours(tmp_path):
    save(_principal(), config_dir=tmp_path)
    back = load(config_dir=tmp_path)
    assert back.quiet_hours == ("22:00", "07:00")
    pref = back.preference_for("anything")
    assert pref.ranked_kinds == ["email"]
    assert pref.urgency_floor == 4


def test_unverified_endpoint_stays_unverified(tmp_path):
    save(_principal(), config_dir=tmp_path)
    assert load(config_dir=tmp_path).endpoint("chat").verified_at is None


def test_absent_record_loads_as_none_not_an_error(tmp_path):
    assert load(config_dir=tmp_path) is None


def test_saved_file_is_owner_only(tmp_path):
    """The record carries addresses and reachability; it is not world-readable."""
    path = save(_principal(), config_dir=tmp_path)
    assert oct(path.stat().st_mode)[-3:] == "600"
