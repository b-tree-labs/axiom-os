# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Trace provider abstraction for Axiom.

A TraceProvider is the seam between Axiom and an observability backend
(Langfuse, OpenTelemetry, a null provider, or an in-memory test double).
Every LLM generation, retrieval, and eval score flows through this surface
so that we can swap backends without rewriting call sites.

Slice 1 (Hello Trace) of the Phase 0 plan: establish the protocol first,
wire a Langfuse implementation next.
"""

from __future__ import annotations

from axiom.infra.tracing.factory import get_trace_provider
from axiom.infra.tracing.in_memory_provider import InMemoryTraceProvider
from axiom.infra.tracing.null_provider import NullTraceProvider
from axiom.infra.tracing.provider import TraceProvider

__all__ = [
    "InMemoryTraceProvider",
    "NullTraceProvider",
    "TraceProvider",
    "get_trace_provider",
]
