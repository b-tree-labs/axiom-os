# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for the promotion pipeline tracer.

Proves that knowledge bubbles up from local → org → community
with traceable events at each transition.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from axiom.agents.learning import AgentKnowledgeStore
from axiom.agents.promotion_tracer import PromotionTracer


@pytest.fixture
def tracer():
    with tempfile.TemporaryDirectory() as tmp:
        yield PromotionTracer(trace_file=Path(tmp) / "traces.jsonl")


@pytest.fixture
def store(tmp_path):
    # Create a minimal repo structure
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".axi" / "agents" / "test-agent").mkdir(parents=True)
    return AgentKnowledgeStore(agent="test-agent", repo_root=repo)


class TestPromotionTracer:
    def test_trace_creates_entry(self, tracer):
        entry = tracer.trace("LEARNED", "p1", agent="neut", confidence="red")
        assert entry["event"] == "LEARNED"
        assert entry["pattern_id"] == "p1"
        assert "ts" in entry

    def test_get_traces(self, tracer):
        tracer.trace("LEARNED", "p1")
        tracer.trace("VERIFIED", "p1")
        tracer.trace("LEARNED", "p2")

        all_traces = tracer.get_traces()
        assert len(all_traces) == 3

        p1_traces = tracer.get_traces("p1")
        assert len(p1_traces) == 2

    def test_promotion_journey(self, tracer):
        tracer.trace("LEARNED", "p1", confidence="red")
        tracer.trace("VERIFIED", "p1", confidence="yellow")
        tracer.trace("PROMOTED", "p1", confidence="green")

        journey = tracer.get_promotion_journey("p1")
        assert journey == ["LEARNED", "VERIFIED", "PROMOTED"]

    def test_assert_full_promotion(self, tracer):
        tracer.trace("LEARNED", "p1")
        assert not tracer.assert_full_promotion("p1")

        tracer.trace("VERIFIED", "p1")
        assert not tracer.assert_full_promotion("p1")

        tracer.trace("PROMOTED", "p1")
        assert tracer.assert_full_promotion("p1")


class TestPromotionPipelineIntegration:
    """End-to-end test: learn → verify → promote with tracing."""

    def test_learn_creates_trace(self, store, tracer, monkeypatch):
        monkeypatch.setattr("axiom.agents.promotion_tracer._tracer", tracer)

        pattern = store.learn(
            category="test",
            signature="test_sig",
            description="Test pattern",
            diagnosis="Diagnosis",
            resolution="Fix it",
        )

        traces = tracer.get_traces(pattern.pattern_id)
        assert len(traces) >= 1
        assert traces[0]["event"] == "LEARNED"
        assert traces[0]["confidence"] == "red"

    def test_verify_creates_trace(self, store, tracer, monkeypatch):
        monkeypatch.setattr("axiom.agents.promotion_tracer._tracer", tracer)

        pattern = store.learn(
            category="test", signature="sig2",
            description="Test", diagnosis="Diag", resolution="Fix",
        )

        store.verify(pattern.pattern_id, success=True, node_id="node-a")

        traces = tracer.get_traces(pattern.pattern_id)
        events = [t["event"] for t in traces]
        assert "LEARNED" in events
        assert "VERIFIED" in events

    def test_full_promotion_lifecycle(self, store, tracer, monkeypatch):
        """Complete journey: RED → YELLOW → GREEN → PROMOTED."""
        monkeypatch.setattr("axiom.agents.promotion_tracer._tracer", tracer)

        pattern = store.learn(
            category="test", signature="lifecycle_sig",
            description="Lifecycle test", diagnosis="Diag", resolution="Fix",
        )
        pid = pattern.pattern_id

        # Verify from 3 different nodes
        store.verify(pid, success=True, node_id="node-a")
        store.verify(pid, success=True, node_id="node-b")
        store.verify(pid, success=True, node_id="node-c")

        journey = tracer.get_promotion_journey(pid)
        assert "LEARNED" in journey
        assert "VERIFIED" in journey
        assert "MULTI_VERIFIED" in journey
        assert "PROMOTED" in journey  # auto-promoted when GREEN

        # Verify the full promotion is traceable
        assert tracer.assert_full_promotion(pid)
