# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for axiom.infra.hooks.priority — pluggable priority strategies."""

from __future__ import annotations

from axiom.infra.hooks import (
    HookSpec,
    ManifestPriorityStrategy,
    PriorityStrategy,
    TrustWeightedStrategy,
    allow,
)


def _spec(event: str, priority: int, source: str) -> HookSpec:
    return HookSpec(
        event=event,
        entry=lambda ctx: allow(),
        priority=priority,
        fail_mode="abort",
        source=source,
    )


class TestManifestPriorityStrategy:
    def test_lower_priority_runs_first(self):
        strat = ManifestPriorityStrategy()
        a = _spec("tool.pre_invoke", priority=200, source="ext_a")
        b = _spec("tool.pre_invoke", priority=50, source="ext_b")
        c = _spec("tool.pre_invoke", priority=100, source="ext_c")
        ordered = strat.order([a, b, c])
        assert [s.priority for s in ordered] == [50, 100, 200]

    def test_tie_broken_by_source_name(self):
        strat = ManifestPriorityStrategy()
        a = _spec("tool.pre_invoke", priority=100, source="z_ext")
        b = _spec("tool.pre_invoke", priority=100, source="a_ext")
        ordered = strat.order([a, b])
        assert [s.source for s in ordered] == ["a_ext", "z_ext"]

    def test_protocol_compliance(self):
        strat = ManifestPriorityStrategy()
        assert isinstance(strat, PriorityStrategy)
        assert strat.name == "manifest_priority"


class TestTrustWeightedStrategy:
    def test_higher_trust_runs_first(self):
        # Trust lookup: signed_ext = 90, user = 30
        trust_lookup = {"signed_ext": 90, "user": 30}.get
        strat = TrustWeightedStrategy(trust_lookup=lambda s: trust_lookup(s, 0))
        a = _spec("tool.pre_invoke", priority=100, source="user")
        b = _spec("tool.pre_invoke", priority=100, source="signed_ext")
        ordered = strat.order([a, b])
        assert [s.source for s in ordered] == ["signed_ext", "user"]

    def test_priority_breaks_ties_within_same_trust(self):
        trust_lookup = lambda s: 50  # noqa: E731 — uniform trust
        strat = TrustWeightedStrategy(trust_lookup=trust_lookup)
        a = _spec("tool.pre_invoke", priority=300, source="ext_a")
        b = _spec("tool.pre_invoke", priority=10, source="ext_b")
        ordered = strat.order([a, b])
        assert [s.priority for s in ordered] == [10, 300]

    def test_protocol_compliance(self):
        strat = TrustWeightedStrategy(trust_lookup=lambda s: 0)
        assert isinstance(strat, PriorityStrategy)
        assert strat.name == "trust_weighted"
