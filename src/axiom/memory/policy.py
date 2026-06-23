# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Four-scope policy coordinate (#38) — π_global, π_u, π_a, π_t.

Per Rezazadeh et al. 2025 (arXiv 2505.18279, §3.1): memory policies
live at four composable scopes. This module provides the coordinate,
resolution order, and named profile application.

Policies are shallow dicts of rules (e.g. `{"read": "allow",
"write": "deny"}`). resolve() merges overrides with the most-
specific scope winning per rule; less-specific scopes contribute
any rules the more-specific scopes don't set.

Precedence (most → least specific):
  time ∧ user > time > user > agent > global

Pure functional — transitions return new coordinates.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class TimeWindow:
    """A time-scoped policy override valid within [start, end)."""

    start: str
    end: str
    policy: dict

    def covers(self, at: str) -> bool:
        return self.start <= at < self.end


@dataclass
class PolicyCoord:
    """Four-scope policy coordinate."""

    global_policy: dict = field(default_factory=dict)
    per_user: dict[str, dict] = field(default_factory=dict)
    per_agent: dict[str, dict] = field(default_factory=dict)
    time_windows: list[TimeWindow] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Builders (return new coord; never mutate)
# ---------------------------------------------------------------------------


def with_global(coord: PolicyCoord, policy: dict) -> PolicyCoord:
    c = deepcopy(coord)
    c.global_policy = dict(policy)
    return c


def with_user(coord: PolicyCoord, user: str, policy: dict) -> PolicyCoord:
    c = deepcopy(coord)
    c.per_user = dict(c.per_user)
    c.per_user[user] = dict(policy)
    return c


def with_agent(coord: PolicyCoord, agent: str, policy: dict) -> PolicyCoord:
    c = deepcopy(coord)
    c.per_agent = dict(c.per_agent)
    c.per_agent[agent] = dict(policy)
    return c


def with_time_window(coord: PolicyCoord, window: TimeWindow) -> PolicyCoord:
    c = deepcopy(coord)
    c.time_windows = [*c.time_windows, window]
    return c


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def _merge_dicts(*dicts: dict) -> dict:
    """Later dicts override earlier. None values skip."""
    out: dict = {}
    for d in dicts:
        if d:
            out.update(d)
    return out


def resolve(
    coord: PolicyCoord,
    user: str,
    agent: str,
    at: str,
) -> dict:
    """Resolve the effective policy dict for a (user, agent, time) point.

    Applies overrides in precedence order: global < agent < user <
    time-window. Merging is shallow (per-rule), so any rule NOT
    touched by a more-specific scope falls through from less-specific
    scopes.
    """
    agent_override = coord.per_agent.get(agent) or {}
    user_override = coord.per_user.get(user) or {}
    time_override: dict = {}
    for w in coord.time_windows:
        if w.covers(at):
            time_override = _merge_dicts(time_override, w.policy)

    return _merge_dicts(
        coord.global_policy,
        agent_override,
        user_override,
        time_override,
    )


# ---------------------------------------------------------------------------
# Named profiles
# ---------------------------------------------------------------------------


@dataclass
class PolicyProfile:
    """A named bundle of policy settings applied as a unit.

    Example: `classroom-default`, `classified-mode`, `researcher`.
    Applies to the global, per-user, per-agent, and time scopes
    of a PolicyCoord in one operation.
    """

    name: str
    global_policy: dict = field(default_factory=dict)
    per_user: dict[str, dict] = field(default_factory=dict)
    per_agent: dict[str, dict] = field(default_factory=dict)
    time_windows: list[TimeWindow] = field(default_factory=list)


def apply_profile(coord: PolicyCoord, profile: PolicyProfile) -> PolicyCoord:
    """Merge a profile into a coordinate. Profile values override existing."""
    c = deepcopy(coord)
    if profile.global_policy:
        c.global_policy = dict(profile.global_policy)
    for u, p in profile.per_user.items():
        c.per_user = dict(c.per_user)
        c.per_user[u] = dict(p)
    for a, p in profile.per_agent.items():
        c.per_agent = dict(c.per_agent)
        c.per_agent[a] = dict(p)
    if profile.time_windows:
        c.time_windows = [*c.time_windows, *profile.time_windows]
    return c
