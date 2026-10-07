# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Calibration & ETA for advanced ingest (spec-rag-ingest-advanced §5).

ETA is computed from this run against this destination, not a hardcoded
throughput. A rolling chunk window keeps the rate (and ETA) responsive to
slowdowns. If calibration itself takes too long, abort — the backend is
unhealthy and the full run will be worse. Clock + embed are injected for
deterministic tests.
"""

from __future__ import annotations

from axiom.rag.ingest_calibration import (
    RollingThroughput,
    calibrate,
    estimate_eta,
)


class _FakeClock:
    def __init__(self, t: float = 0.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


def test_rolling_throughput_overall_rate():
    clock = _FakeClock(0.0)
    rt = RollingThroughput(window_chunks=200, clock=clock)
    rt.record(10)  # t=0
    clock.t = 1.0
    rt.record(10)  # t=1
    clock.t = 2.0
    assert rt.chunks_per_sec() == 10.0  # 20 chunks over 2s


def test_rolling_throughput_evicts_old_samples_to_track_recent_rate():
    clock = _FakeClock(0.0)
    rt = RollingThroughput(window_chunks=100, clock=clock)
    rt.record(10)  # slow start at t=0
    clock.t = 1.0
    rt.record(100)  # burst at t=1
    clock.t = 2.0
    # The slow start is evicted; rate reflects the recent 100 chunks over 1s.
    assert rt.chunks_per_sec() == 100.0


def test_estimate_eta_applies_safety_multiplier():
    assert estimate_eta(100, 10.0, multiplier=1.3) == 13.0


def test_estimate_eta_none_when_no_throughput():
    assert estimate_eta(100, 0.0) is None


def test_calibrate_happy_path():
    clock = _FakeClock(0.0)
    sizes = iter([100, 100, 100, 100])

    def embed_one():
        clock.t += 0.5
        return next(sizes)

    res = calibrate(4, embed_one=embed_one, clock=clock, abort_after_s=60.0)
    assert res.chunks == 4
    assert res.elapsed_s == 2.0
    assert res.chunks_per_sec == 2.0
    assert res.mean_chunk_bytes == 100.0
    assert res.aborted is False


def test_calibrate_aborts_when_too_slow():
    clock = _FakeClock(0.0)

    def slow_embed():
        clock.t += 40.0  # absurdly slow backend
        return 100

    res = calibrate(4, embed_one=slow_embed, clock=clock, abort_after_s=60.0)
    assert res.aborted is True
    assert res.chunks == 2  # stopped after crossing the 60s budget
    assert res.reason is not None
