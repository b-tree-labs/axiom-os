# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Guided communications-config templates + questions (ADR-074).

The Axiom configuration assistant (AXI) uses these to help a user assemble a
``ChannelPolicy`` without hand-writing rules: it offers *generally useful
situation templates* given the channels the user actually has, and asks the
questions needed to fill them in. NL (typed or verbalized) maps a user's
words onto these answers; this module is the structured target + question set.
"""

from __future__ import annotations

from .channel_policy import ChannelPolicy, ChannelRule, UserStatus


def _first_present(prefer: list[str], available: set[str]) -> tuple[str, ...]:
    return tuple(c for c in prefer if c in available)


def situation_templates(available: set[str]) -> dict[str, ChannelRule | None]:
    """Generally-useful scenario rules, parameterized by available channels.
    A template is None when the channels it needs aren't available."""
    mobile = _first_present(["imessage", "sms"], available)
    async_ = _first_present(["slack", "teams", "inbox"], available)
    quiet = _first_present(["inbox", "imessage"], available)
    return {
        # urgent always breaks through to mobile
        "critical_breakthrough": ChannelRule(channels=mobile or async_, min_priority="critical"),
        # presenting / focus → terse, mobile, low-interrupt
        "presenting": ChannelRule(channels=mobile or async_, status=(UserStatus.PRESENTING, UserStatus.FOCUS)),
        # in a meeting → async, no phone buzz
        "in_meeting": ChannelRule(channels=async_, status=(UserStatus.IN_MEETING,)),
        # off-hours / vacation → defer to async/quiet
        "off_hours": ChannelRule(channels=quiet or async_, status=(UserStatus.OFF_HOURS, UserStatus.VACATION)),
        # away from desk → mobile
        "away": ChannelRule(channels=mobile or async_, status=(UserStatus.AWAY,)),
    }


def config_questions(available: set[str]) -> list[str]:
    """The questions AXI asks to assemble a policy, grounded in real channels."""
    chans = ", ".join(sorted(available)) or "(none configured yet)"
    return [
        f"Your available channels are: {chans}. Which should be your default for routine updates?",
        "Which channel must always reach you for *critical* alerts (it'll break through any status)?",
        "When you're presenting or heads-down, should I hold non-urgent messages and only text the urgent ones?",
        "After hours or on vacation, should routine messages wait in an async inbox until you're back?",
        "Any topics or projects that should always route to a specific channel (e.g. incidents → Slack)?",
        "Anyone whose messages should reach you on a particular channel regardless of status?",
    ]


def policy_from_answers(answers: dict, available: set[str]) -> ChannelPolicy:
    """Assemble a ChannelPolicy from answered template choices.

    ``answers`` keys (all optional): default (channel), critical (channel),
    presenting (bool), defer_off_hours (bool), topic_routes ({topic: channel}).
    AXI/NL fills these; this turns them into ordered rules (breakthrough first).
    """
    tmpl = situation_templates(available)
    rules: list[ChannelRule] = []
    if answers.get("critical"):
        rules.append(ChannelRule(channels=(answers["critical"],), min_priority="critical"))
    elif tmpl["critical_breakthrough"] and tmpl["critical_breakthrough"].channels:
        rules.append(tmpl["critical_breakthrough"])
    if answers.get("presenting", True) and tmpl["presenting"] and tmpl["presenting"].channels:
        rules.append(tmpl["presenting"])
    if answers.get("defer_off_hours", True) and tmpl["off_hours"] and tmpl["off_hours"].channels:
        rules.append(tmpl["off_hours"])
    for topic, chan in (answers.get("topic_routes") or {}).items():
        rules.append(ChannelRule(channels=(chan,), topics=(topic,)))
    default = answers.get("default") or next(iter(_first_present(["slack", "teams", "inbox", "imessage"], available)), "inbox")
    return ChannelPolicy(default_channels=(default,), rules=tuple(rules))


__all__ = ["situation_templates", "config_questions", "policy_from_answers"]
