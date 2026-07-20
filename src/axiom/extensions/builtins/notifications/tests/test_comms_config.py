# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Guided comms-config: situation templates, the questions AXI asks, and
assembling a policy from answers (ADR-074)."""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.notifications.channel_policy import CommsContext, UserStatus
from axiom.extensions.builtins.notifications.comms_config import (
    config_questions,
    policy_from_answers,
    situation_templates,
)


def test_templates_adapt_to_available_channels():
    t = situation_templates({"slack", "imessage", "inbox"})
    # presenting prefers mobile (imessage) when present
    assert t["presenting"].channels[0] == "imessage"
    # with no mobile channel, presenting falls back to async
    t2 = situation_templates({"slack", "inbox"})
    assert "imessage" not in t2["presenting"].channels and "sms" not in t2["presenting"].channels


def test_config_questions_reference_real_channels_and_cover_scenarios():
    qs = config_questions({"slack", "sms"})
    blob = " ".join(qs).lower()
    assert "slack" in blob and "sms" in blob
    for essential in ("critical", "present", "after hours", "topic", "default"):
        assert essential in blob


def test_policy_from_answers_builds_usable_policy():
    pol = policy_from_answers(
        {"default": "slack", "critical": "sms", "presenting": True, "defer_off_hours": True,
         "topic_routes": {"incident": "slack"}},
        available={"slack", "sms", "imessage", "inbox"},
    )
    # critical breaks through to sms
    assert pol.resolve(CommsContext(status=UserStatus.VACATION, priority="critical"))[0] == "sms"
    # routine default is slack
    assert pol.resolve(CommsContext(status=UserStatus.AVAILABLE)) == ["slack"]
    # off-hours defers
    assert pol.resolve(CommsContext(status=UserStatus.OFF_HOURS)) != ["slack"]
    # topic routes
    assert pol.resolve(CommsContext(topic="incident")) == ["slack"]


def test_skill_configure_returns_questions_then_policy():
    reg = pytest.importorskip("axiom.extensions.builtins.notifications.skills.comms")
    # no answers → returns the guided questions
    q = reg.configure({"available": ["slack", "sms"]}, ctx=None)
    assert q.ok and q.value["questions"]
    # with answers → returns an assembled policy summary
    p = reg.configure({"available": ["slack", "sms"], "answers": {"default": "slack", "critical": "sms"}}, ctx=None)
    assert p.ok and p.value["default_channels"] == ["slack"]
    assert any(r.get("min_priority") == "critical" for r in p.value["rules"])
