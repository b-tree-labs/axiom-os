# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""InMemoryTraceProvider — test double that captures events for assertion."""

from __future__ import annotations


def test_in_memory_captures_trace_start() -> None:
    from axiom.infra.tracing import InMemoryTraceProvider

    p = InMemoryTraceProvider()
    tid = p.start_trace("research-loop", session="s1")
    assert len(p.traces) == 1
    assert p.traces[0]["id"] == tid
    assert p.traces[0]["name"] == "research-loop"
    assert p.traces[0]["metadata"] == {"session": "s1"}


def test_in_memory_captures_generation_and_score() -> None:
    from axiom.infra.tracing import InMemoryTraceProvider

    p = InMemoryTraceProvider()
    tid = p.start_trace("t")
    p.log_generation(tid, model="bonsai-1.7b", prompt="hi", output="hello")
    p.log_retrieval(tid, query="q", results=[{"doc": 1}])
    p.score(tid, name="faithfulness", value=0.87)

    assert len(p.generations) == 1
    assert p.generations[0]["model"] == "bonsai-1.7b"
    assert len(p.retrievals) == 1
    assert p.scores[0] == {
        "trace_id": tid,
        "name": "faithfulness",
        "value": 0.87,
        "metadata": {},
    }


def test_in_memory_flush_is_callable() -> None:
    from axiom.infra.tracing import InMemoryTraceProvider

    p = InMemoryTraceProvider()
    p.flush()  # must not raise
