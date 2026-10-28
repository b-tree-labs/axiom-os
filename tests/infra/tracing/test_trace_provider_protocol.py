# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Slice 1 — Hello Trace: TraceProvider protocol surface.

First failing test for Phase 0. Establishes the protocol that every trace
backend (Langfuse, null, in-memory test double) must satisfy.
"""

from __future__ import annotations


def test_trace_provider_protocol_is_importable() -> None:
    from axiom.infra.tracing import TraceProvider

    assert TraceProvider is not None


def test_trace_provider_has_required_methods() -> None:
    from axiom.infra.tracing import TraceProvider

    required = {"start_trace", "log_generation", "log_retrieval", "score", "flush"}
    members = {name for name in dir(TraceProvider) if not name.startswith("_")}
    missing = required - members
    assert not missing, f"TraceProvider missing required methods: {missing}"
