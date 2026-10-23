# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""In-memory TraceProvider for tests. Captures events as dicts on the instance."""

from __future__ import annotations

import uuid
from typing import Any


class InMemoryTraceProvider:
    """Captures trace events on instance attributes for test assertions."""

    def __init__(self) -> None:
        self.traces: list[dict[str, Any]] = []
        self.generations: list[dict[str, Any]] = []
        self.retrievals: list[dict[str, Any]] = []
        self.scores: list[dict[str, Any]] = []
        self.flush_count: int = 0

    def start_trace(self, name: str, **metadata: Any) -> str:
        tid = uuid.uuid4().hex[:16]
        self.traces.append({"id": tid, "name": name, "metadata": metadata})
        return tid

    def log_generation(
        self, trace_id: str, *, model: str, prompt: Any, output: Any, **metadata: Any
    ) -> None:
        self.generations.append(
            {
                "trace_id": trace_id,
                "model": model,
                "prompt": prompt,
                "output": output,
                "metadata": metadata,
            }
        )

    def log_retrieval(
        self, trace_id: str, *, query: str, results: list[Any], **metadata: Any
    ) -> None:
        self.retrievals.append(
            {
                "trace_id": trace_id,
                "query": query,
                "results": results,
                "metadata": metadata,
            }
        )

    def score(self, trace_id: str, *, name: str, value: float, **metadata: Any) -> None:
        self.scores.append(
            {
                "trace_id": trace_id,
                "name": name,
                "value": value,
                "metadata": metadata,
            }
        )

    def flush(self) -> None:
        self.flush_count += 1

    # -- test-inspection helpers (not part of the TraceProvider contract) ----

    def get_trace(self, trace_id: str) -> dict[str, Any] | None:
        """Lookup a trace by ID. Returns None if not found."""
        return next((t for t in self.traces if t["id"] == trace_id), None)

    def get_generations(self, trace_id: str) -> list[dict[str, Any]]:
        """Return all generations for a given trace."""
        return [g for g in self.generations if g["trace_id"] == trace_id]

    def get_retrievals(self, trace_id: str) -> list[dict[str, Any]]:
        """Return all retrievals for a given trace."""
        return [r for r in self.retrievals if r["trace_id"] == trace_id]

    def get_traces_by_metadata(self, key: str, value: Any) -> list[dict[str, Any]]:
        """Return all traces whose metadata[key] == value."""
        return [t for t in self.traces if t.get("metadata", {}).get(key) == value]
