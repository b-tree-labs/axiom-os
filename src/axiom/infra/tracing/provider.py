# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""TraceProvider protocol.

Minimum surface a trace backend must implement. Implementations:
  - LangfuseTraceProvider (Slice 1 — real backend)
  - NullTraceProvider (no-op, always available)
  - InMemoryTraceProvider (test double; asserts over captured events)

Keep this surface small on purpose. Every method added here must be
implemented by every backend, so add only what every call site needs.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class TraceProvider(Protocol):
    """Observability backend seam for LLM generations, retrievals, and evals."""

    def start_trace(self, name: str, **metadata: Any) -> str:
        """Begin a logical trace. Returns an opaque trace id."""
        ...

    def log_generation(
        self,
        trace_id: str,
        *,
        model: str,
        prompt: Any,
        output: Any,
        **metadata: Any,
    ) -> None:
        """Record a single LLM generation under a trace."""
        ...

    def log_retrieval(
        self,
        trace_id: str,
        *,
        query: str,
        results: list[Any],
        **metadata: Any,
    ) -> None:
        """Record a retrieval (RAG hit) under a trace."""
        ...

    def score(
        self,
        trace_id: str,
        *,
        name: str,
        value: float,
        **metadata: Any,
    ) -> None:
        """Attach an eval score to a trace."""
        ...

    def flush(self) -> None:
        """Force-flush any buffered events. Called at process exit."""
        ...
