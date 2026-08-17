# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Preferred channels — scenario-conditioned, per extension + agent (ADR-074).

An agent's *communications* config: which channel(s) to use, resolved
**dynamically** from context — the topic, the project, the time of day, and
the user's status (in a meeting, off-hours, on vacation, presenting, away).
This sits above the recipient/classification/priority ordering in
``preferences.py``; it answers "given the situation right now, which channel?"

The structured ``ChannelPolicy`` is the compile target; it can be authored by
hand or emitted from a natural-language instruction (see ``comms_config`` —
"when I'm presenting, only text me; defer non-urgent after hours").
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

_PRIORITY_ORDER = {"low": 0, "normal": 1, "high": 2, "critical": 3}


class UserStatus(str, Enum):
    AVAILABLE = "available"
    IN_MEETING = "in_meeting"
    PRESENTING = "presenting"
    FOCUS = "focus"
    OFF_HOURS = "off_hours"
    VACATION = "vacation"
    AWAY = "away"          # not at desk


@dataclass(frozen=True)
class CommsContext:
    """The situation a message is being sent in — the dynamism inputs."""

    agent: str | None = None
    extension: str | None = None
    recipient: str | None = None
    project: str | None = None
    topic: str | None = None
    hour: int | None = None              # local hour 0-23
    status: UserStatus = UserStatus.AVAILABLE
    priority: str = "normal"


@dataclass(frozen=True)
class ChannelRule:
    """A scenario → ordered channels. Unset conditions are wildcards; all set
    conditions must match (AND). First matching rule in a policy wins, so put
    breakthrough rules (e.g. critical) first."""

    channels: tuple[str, ...]
    status: tuple[UserStatus, ...] | None = None
    topics: tuple[str, ...] | None = None
    projects: tuple[str, ...] | None = None
    people: tuple[str, ...] | None = None
    hours: tuple[int, ...] | None = None
    min_priority: str | None = None

    def matches(self, ctx: CommsContext) -> bool:
        if self.status is not None and ctx.status not in self.status:
            return False
        if self.topics is not None and (ctx.topic or "") not in self.topics:
            return False
        if self.projects is not None and (ctx.project or "") not in self.projects:
            return False
        if self.people is not None and (ctx.recipient or "") not in self.people:
            return False
        if self.hours is not None and ctx.hour not in self.hours:
            return False
        if self.min_priority is not None:
            if _PRIORITY_ORDER.get(ctx.priority, 1) < _PRIORITY_ORDER.get(self.min_priority, 3):
                return False
        return True


@dataclass(frozen=True)
class ChannelPolicy:
    default_channels: tuple[str, ...]
    rules: tuple[ChannelRule, ...] = ()

    def resolve(self, ctx: CommsContext, *, available: set[str] | None = None) -> list[str]:
        chosen: tuple[str, ...] = self.default_channels
        for rule in self.rules:
            if rule.matches(ctx):
                chosen = rule.channels
                break
        out = list(chosen)
        if available is not None:
            out = [c for c in out if c in available]
        return out


class ChannelPolicyStore:
    """Per (extension, agent) communications policies."""

    def __init__(self) -> None:
        self._by_key: dict[tuple[str, str], ChannelPolicy] = {}

    def put(self, extension: str, agent: str, policy: ChannelPolicy) -> None:
        self._by_key[(extension, agent)] = policy

    def get(self, extension: str, agent: str) -> ChannelPolicy | None:
        return self._by_key.get((extension, agent))

    def resolve(
        self,
        extension: str,
        agent: str,
        ctx: CommsContext,
        *,
        available: set[str] | None = None,
        default: ChannelPolicy | None = None,
    ) -> list[str]:
        policy = self.get(extension, agent) or default
        if policy is None:
            return []
        return policy.resolve(ctx, available=available)


__all__ = [
    "UserStatus",
    "CommsContext",
    "ChannelRule",
    "ChannelPolicy",
    "ChannelPolicyStore",
]
