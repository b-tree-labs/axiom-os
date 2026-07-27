# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""No-op TraceProvider. Always available, never fails, records nothing."""

from __future__ import annotations

import uuid
from typing import Any


class NullTraceProvider:
    """Trace provider that discards every event. Safe default when no backend is configured."""

    def start_trace(self, name: str, **metadata: Any) -> str:
        return uuid.uuid4().hex[:16]

    def log_generation(
        self, trace_id: str, *, model: str, prompt: Any, output: Any, **metadata: Any
    ) -> None:
        return None

    def log_retrieval(
        self, trace_id: str, *, query: str, results: list[Any], **metadata: Any
    ) -> None:
        return None

    def score(
        self, trace_id: str, *, name: str, value: float, **metadata: Any
    ) -> None:
        return None

    def flush(self) -> None:
        return None
