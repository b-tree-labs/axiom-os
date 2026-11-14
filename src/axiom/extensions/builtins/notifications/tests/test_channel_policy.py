# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Scenario-conditioned preferred channels (ADR-074): per extension+agent,
resolved dynamically from context — topic, project, time of day, and the
user's status (meeting / off-hours / vacation / presenting / away)."""

from __future__ import annotations

from axiom.extensions.builtins.notifications.channel_policy import (
    ChannelPolicy,
    ChannelPolicyStore,
    ChannelRule,
    CommsContext,
    UserStatus,
)


def _policy():
    return ChannelPolicy(
        default_channels=("slack",),
        rules=(
            # Breakthrough first: critical always reaches mobile, any status.
            ChannelRule(channels=("sms", "imessage"), min_priority="critical"),
            # Presenting → terse mobile only.
            ChannelRule(channels=("imessage", "sms"), status=(UserStatus.PRESENTING,)),
            # Off-hours / vacation → defer to async inbox.
            ChannelRule(channels=("inbox",), status=(UserStatus.OFF_HOURS, UserStatus.VACATION)),
            # Topic routing.
            ChannelRule(channels=("slack",), topics=("incident", "deploy")),
        ),
    )


def test_default_when_no_rule_matches():
    p = _policy()
    assert p.resolve(CommsContext(status=UserStatus.AVAILABLE)) == ["slack"]


def test_presenting_prefers_mobile():
    p = _policy()
    assert p.resolve(CommsContext(status=UserStatus.PRESENTING))[0] == "imessage"


def test_off_hours_defers_to_inbox():
    p = _policy()
    assert p.resolve(CommsContext(status=UserStatus.OFF_HOURS)) == ["inbox"]


def test_critical_breaks_through_even_on_vacation():
    p = _policy()
    # earlier rule (vacation→inbox) matches first; ensure critical rule wins by ordering
    out = p.resolve(CommsContext(status=UserStatus.VACATION, priority="critical"))
    assert out[0] in ("sms", "imessage")


def test_topic_routing():
    p = _policy()
    assert p.resolve(CommsContext(status=UserStatus.AVAILABLE, topic="incident")) == ["slack"]


def test_resolution_intersects_with_available_channels():
    p = _policy()
    # presenting wants [imessage, sms]; only sms is enabled → sms
    out = p.resolve(CommsContext(status=UserStatus.PRESENTING), available={"sms", "slack"})
    assert out == ["sms"]


def test_policy_store_is_per_extension_and_agent():
    store = ChannelPolicyStore()
    store.put("diagnostics", "TRIAGE", _policy())
    assert store.get("diagnostics", "TRIAGE") is not None
    assert store.get("diagnostics", "OTHER") is None
    # resolve falls back to a global default policy when none registered
    out = store.resolve("unknown", "nobody", CommsContext(), default=ChannelPolicy(default_channels=("inbox",)))
    assert out == ["inbox"]
