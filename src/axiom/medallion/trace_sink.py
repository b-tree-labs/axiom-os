# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""BronzeTraceSink — TraceProvider that lands events into the bronze layer.

Satisfies the TraceProvider protocol so any Axiom code emitting traces
can instead (or in addition to) land them in bronze for downstream
medallion processing.
"""

from __future__ import annotations

import uuid
from typing import Any

from axiom.medallion.bronze import BronzeStore


class BronzeTraceSink:
    """Trace provider whose flush lands events in a BronzeStore."""

    def __init__(self, *, bronze: BronzeStore, day: str) -> None:
        self._bronze = bronze
        self._day = day

    def start_trace(self, name: str, **metadata: Any) -> str:
        tid = uuid.uuid4().hex[:16]
        self._bronze.append(
            source="traces",
            day=self._day,
            row={"id": tid, "name": name, "metadata": metadata},
        )
        return tid

    def log_generation(
        self, trace_id: str, *, model: str, prompt: Any, output: Any, **metadata: Any
    ) -> None:
        self._bronze.append(
            source="generations",
            day=self._day,
            row={
                "trace_id": trace_id,
                "model": model,
                "prompt": prompt,
                "output": output,
                "metadata": metadata,
            },
        )

    def log_retrieval(
        self, trace_id: str, *, query: str, results: list[Any], **metadata: Any
    ) -> None:
        self._bronze.append(
            source="retrievals",
            day=self._day,
            row={
                "trace_id": trace_id,
                "query": query,
                "results": results,
                "metadata": metadata,
            },
        )

    def score(
        self, trace_id: str, *, name: str, value: float, **metadata: Any
    ) -> None:
        self._bronze.append(
            source="scores",
            day=self._day,
            row={
                "trace_id": trace_id,
                "name": name,
                "value": value,
                "metadata": metadata,
            },
        )

    def flush(self) -> None:
        # No-op: appends are already durable in the in-memory bronze.
        return None
