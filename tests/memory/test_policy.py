# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""TDD tests for four-scope policy coordinate (#38).

Per Collaborative Memory paper §3.1: policies live at four scopes
that compose.

  π_global — platform default
  π_u      — per-principal override
  π_a      — per-agent override
  π_t      — time-scoped override

resolve(coord, user, agent, at) picks the most-specific applicable
policy. Precedence (most → least specific): time ∧ user ∧ agent >
user ∧ agent > user > agent > global.
"""

from __future__ import annotations


class TestCoordinateLookup:
    def test_global_default_when_no_overrides(self):
        from axiom.memory.policy import PolicyCoord, resolve

        coord = PolicyCoord(global_policy={"read": "allow"})
        p = resolve(coord, user="u1", agent="a1", at="2026-04-17T00:00:00Z")
        assert p == {"read": "allow"}

    def test_user_override_beats_global(self):
        from axiom.memory.policy import PolicyCoord, resolve

        coord = PolicyCoord(
            global_policy={"read": "allow"},
            per_user={"u1": {"read": "deny"}},
        )
        assert resolve(coord, "u1", "a1", "2026-04-17T00:00:00Z") == {"read": "deny"}
        assert resolve(coord, "u2", "a1", "2026-04-17T00:00:00Z") == {"read": "allow"}

    def test_agent_override_beats_global(self):
        from axiom.memory.policy import PolicyCoord, resolve

        coord = PolicyCoord(
            global_policy={"read": "allow"},
            per_agent={"a1": {"read": "deny"}},
        )
        assert resolve(coord, "u1", "a1", "2026-04-17T00:00:00Z") == {"read": "deny"}
        assert resolve(coord, "u1", "a2", "2026-04-17T00:00:00Z") == {"read": "allow"}

    def test_user_beats_agent_when_both_match(self):
        """User override is more specific than agent override."""
        from axiom.memory.policy import PolicyCoord, resolve

        coord = PolicyCoord(
            global_policy={"read": "allow"},
            per_agent={"a1": {"read": "deny"}},
            per_user={"u1": {"read": "allow-with-redaction"}},
        )
        p = resolve(coord, "u1", "a1", "2026-04-17T00:00:00Z")
        assert p == {"read": "allow-with-redaction"}

    def test_time_scope_beats_user_when_applicable(self):
        from axiom.memory.policy import PolicyCoord, TimeWindow, resolve

        coord = PolicyCoord(
            global_policy={"read": "allow"},
            per_user={"u1": {"read": "deny"}},
            time_windows=[
                TimeWindow(
                    start="2026-04-17T00:00:00Z",
                    end="2026-04-17T23:59:59Z",
                    policy={"read": "audit-only"},
                ),
            ],
        )
        inside = resolve(coord, "u1", "a1", "2026-04-17T12:00:00Z")
        assert inside == {"read": "audit-only"}
        outside = resolve(coord, "u1", "a1", "2026-04-18T12:00:00Z")
        assert outside == {"read": "deny"}


class TestMerging:
    """Policies can merge field-by-field, not just replace whole dict."""

    def test_shallow_merge(self):
        from axiom.memory.policy import PolicyCoord, resolve

        coord = PolicyCoord(
            global_policy={"read": "allow", "write": "deny"},
            per_user={"u1": {"read": "deny"}},  # only read override
        )
        p = resolve(coord, "u1", "a1", "2026-04-17T00:00:00Z")
        # user's read override applies; write falls through from global
        assert p == {"read": "deny", "write": "deny"}


class TestCoordinateBuilders:
    def test_empty_coord(self):
        from axiom.memory.policy import PolicyCoord

        coord = PolicyCoord()
        assert coord.global_policy == {}
        assert coord.per_user == {}
        assert coord.per_agent == {}
        assert coord.time_windows == []

    def test_with_global(self):
        from axiom.memory.policy import PolicyCoord, with_global

        coord = with_global(PolicyCoord(), {"read": "allow"})
        assert coord.global_policy == {"read": "allow"}

    def test_with_user(self):
        from axiom.memory.policy import PolicyCoord, with_user

        coord = with_user(PolicyCoord(), "u1", {"read": "deny"})
        assert coord.per_user == {"u1": {"read": "deny"}}

    def test_immutability_of_builders(self):
        from axiom.memory.policy import PolicyCoord, with_user

        c1 = PolicyCoord()
        c2 = with_user(c1, "u1", {"read": "deny"})
        assert c1.per_user == {}  # original unchanged
        assert c2.per_user == {"u1": {"read": "deny"}}


class TestNamedProfile:
    """Named policy profiles (per trust-policy-profiles memory):
    coordinate-level configs that can be applied as a unit."""

    def test_apply_profile_merges_all_scopes(self):
        from axiom.memory.policy import PolicyCoord, PolicyProfile, apply_profile

        profile = PolicyProfile(
            name="classroom-default",
            global_policy={"read": "allow", "write": "deny"},
            per_agent={"axi": {"read": "allow", "write": "allow"}},
        )
        coord = apply_profile(PolicyCoord(), profile)
        assert coord.global_policy == {"read": "allow", "write": "deny"}
        assert coord.per_agent == {"axi": {"read": "allow", "write": "allow"}}
