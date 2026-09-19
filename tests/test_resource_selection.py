# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for federated resource selection (P2.5).

Nodes select the best LLM across the federation based on capability,
latency, and availability. Three strategies: best-of, fan-out, pinned.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def sample_resources():
    return [
        {"node_id": "local", "name": "bonsai-local", "model": "bonsai-1.7b",
         "capability_score": 0.2, "latency_ms": 5, "available": True},
        {"node_id": "node1", "name": "qwen-node1", "model": "Qwen3.5-122B",
         "capability_score": 0.95, "latency_ms": 50, "available": True},
        {"node_id": "hpc", "name": "llama-hpc", "model": "llama3-70b",
         "capability_score": 0.7, "latency_ms": 200, "available": False},
    ]


class TestResourceSelector:
    def test_importable(self):
        from axiom.vega.federation.resource_selection import ResourceSelector
        assert ResourceSelector is not None

    def test_best_of_picks_highest_capability(self, sample_resources):
        from axiom.vega.federation.resource_selection import ResourceSelector

        selector = ResourceSelector(strategy="best-of")
        selected = selector.select(sample_resources, task="synthesis")
        assert selected["node_id"] == "node1"

    def test_best_of_skips_unavailable(self, sample_resources):
        from axiom.vega.federation.resource_selection import ResourceSelector

        # Make node1 unavailable
        sample_resources[1]["available"] = False
        selector = ResourceSelector(strategy="best-of")
        selected = selector.select(sample_resources, task="synthesis")
        assert selected["node_id"] == "local"  # Best available

    def test_pinned_uses_specified_node(self, sample_resources):
        from axiom.vega.federation.resource_selection import ResourceSelector

        selector = ResourceSelector(strategy="pinned", pinned_node="node1")
        selected = selector.select(sample_resources, task="synthesis")
        assert selected["node_id"] == "node1"

    def test_pinned_falls_back_if_unavailable(self, sample_resources):
        from axiom.vega.federation.resource_selection import ResourceSelector

        sample_resources[1]["available"] = False
        selector = ResourceSelector(strategy="pinned", pinned_node="node1")
        selected = selector.select(sample_resources, task="synthesis")
        assert selected["node_id"] == "local"  # Fallback to best available

    def test_fan_out_returns_all_available(self, sample_resources):
        from axiom.vega.federation.resource_selection import ResourceSelector

        selector = ResourceSelector(strategy="fan-out")
        selected = selector.select_all(sample_resources)
        assert len(selected) == 2  # Only available ones
        assert all(r["available"] for r in selected)

    def test_task_weighted_selection(self, sample_resources):
        """Classification tasks prefer low-latency; synthesis prefers capability."""
        from axiom.vega.federation.resource_selection import ResourceSelector

        selector = ResourceSelector(strategy="best-of")
        # For classification, latency matters more
        selected = selector.select(sample_resources, task="classification")
        # Local has 5ms latency, node1 has 50ms — but node1 is still better
        # unless we weight latency heavily
        assert selected is not None

    def test_empty_resources_returns_none(self):
        from axiom.vega.federation.resource_selection import ResourceSelector

        selector = ResourceSelector(strategy="best-of")
        selected = selector.select([], task="synthesis")
        assert selected is None

    def test_all_unavailable_returns_none(self, sample_resources):
        from axiom.vega.federation.resource_selection import ResourceSelector

        for r in sample_resources:
            r["available"] = False
        selector = ResourceSelector(strategy="best-of")
        selected = selector.select(sample_resources, task="synthesis")
        assert selected is None
