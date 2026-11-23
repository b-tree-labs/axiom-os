# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Factory: select a TraceProvider implementation from config."""

from __future__ import annotations

from typing import Any

from axiom.infra.tracing.in_memory_provider import InMemoryTraceProvider
from axiom.infra.tracing.null_provider import NullTraceProvider
from axiom.infra.tracing.provider import TraceProvider


def get_trace_provider(config: dict[str, Any] | None = None) -> TraceProvider:
    """Return a TraceProvider for the configured backend.

    Recognized backends:
      - "null" (default) — no-op
      - "in_memory" — captures events for tests
      - "langfuse" — real Langfuse backend (Slice 1 continuation)
    """
    cfg = config or {}
    backend = cfg.get("backend", "null")

    if backend == "null":
        return NullTraceProvider()
    if backend == "in_memory":
        return InMemoryTraceProvider()
    if backend == "langfuse":
        from axiom.infra.tracing.langfuse_provider import LangfuseTraceProvider

        return LangfuseTraceProvider(
            transport=cfg.get("transport"),
            public_key=cfg.get("public_key"),
            secret_key=cfg.get("secret_key"),
            host=cfg.get("host"),
        )

    raise ValueError(f"unknown trace backend: {backend!r}")
