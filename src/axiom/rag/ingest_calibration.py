# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Calibration & ETA for advanced ingest (spec-rag-ingest-advanced §5).

Hardcoded throughput ages badly across embedding backends, hardware, and
networks, so ETA is measured from this run against this destination. A rolling
chunk window keeps the rate responsive to slowdowns (network throttle, backend
pressure). If calibration itself blows a time budget, abort — the full run will
only be worse. Clock + embed are injected so the logic is deterministic.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass

_DEFAULT_WINDOW_CHUNKS = 200
_DEFAULT_ETA_MULTIPLIER = 1.3  # tail-latency safety factor (shown in the UI)


class RollingThroughput:
    """Chunks/sec over a rolling window of recent samples.

    Old samples are evicted once the buffered chunk count would exceed the
    window, so the reported rate reflects recent behavior rather than the whole
    run — a slowdown shows up in the ETA within seconds.
    """

    def __init__(self, window_chunks: int = _DEFAULT_WINDOW_CHUNKS, *, clock=time.monotonic):
        self._window = window_chunks
        self._clock = clock
        self._samples: deque[tuple[float, int]] = deque()
        self._total = 0

    def record(self, chunks: int) -> None:
        self._samples.append((self._clock(), chunks))
        self._total += chunks
        # Evict oldest while doing so still leaves the window covered.
        while len(self._samples) > 1 and (self._total - self._samples[0][1]) >= self._window:
            _, c = self._samples.popleft()
            self._total -= c

    def chunks_per_sec(self) -> float:
        if not self._samples:
            return 0.0
        elapsed = self._clock() - self._samples[0][0]
        if elapsed <= 0:
            return 0.0
        return self._total / elapsed


def estimate_eta(
    remaining_chunks: int,
    chunks_per_sec: float,
    multiplier: float = _DEFAULT_ETA_MULTIPLIER,
) -> float | None:
    """Projected seconds remaining, or None when throughput is unknown."""
    if chunks_per_sec <= 0:
        return None
    return (remaining_chunks / chunks_per_sec) * multiplier


@dataclass
class CalibrationResult:
    chunks: int
    elapsed_s: float
    chunks_per_sec: float
    mean_chunk_bytes: float
    aborted: bool
    reason: str | None = None


def calibrate(
    sample: int,
    *,
    embed_one,
    clock=time.monotonic,
    abort_after_s: float = 60.0,
) -> CalibrationResult:
    """Run a sample of chunks end-to-end to measure real throughput.

    ``embed_one()`` performs one chunk's chunk→embed→write and returns its
    byte size. Aborts (without finishing the sample) if the elapsed budget is
    exceeded — that means the backend or store is unhealthy.
    """
    start = clock()
    total_bytes = 0
    done = 0
    aborted = False
    reason: str | None = None

    for _ in range(sample):
        total_bytes += embed_one()
        done += 1
        if clock() - start > abort_after_s:
            aborted = True
            reason = (
                f"calibration exceeded {abort_after_s:.0f}s after {done} chunks — "
                "embedding backend or store is unhealthy; the full run would be worse"
            )
            break

    elapsed = clock() - start
    cps = done / elapsed if elapsed > 0 else 0.0
    mean = total_bytes / done if done else 0.0
    return CalibrationResult(done, elapsed, cps, mean, aborted, reason)
