# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""NullTraceProvider — no-op backend, always available."""

from __future__ import annotations


def test_null_provider_satisfies_protocol() -> None:
    from axiom.infra.tracing import NullTraceProvider, TraceProvider

    assert isinstance(NullTraceProvider(), TraceProvider)


def test_null_provider_returns_trace_id() -> None:
    from axiom.infra.tracing import NullTraceProvider

    p = NullTraceProvider()
    tid = p.start_trace("hello", user="ben")
    assert isinstance(tid, str) and tid


def test_null_provider_methods_are_noops() -> None:
    from axiom.infra.tracing import NullTraceProvider

    p = NullTraceProvider()
    tid = p.start_trace("t")
    p.log_generation(tid, model="m", prompt="p", output="o")
    p.log_retrieval(tid, query="q", results=[])
    p.score(tid, name="faithfulness", value=0.9)
    p.flush()
