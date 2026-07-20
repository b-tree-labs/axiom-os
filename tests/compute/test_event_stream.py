# Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Phase 3a — live event stream + watch condition primitives.

Per Twin Toolkit Demo Spec §5.2.5 (Seam G) + Twin OS Build March:

- KernelEvent: structured records emitted during kernel execution
  (cycle_complete, tally_update, lost_particle, warning_emitted, ...)
- CodeAdapter gains event_stream(determinism_state, kernel_options) yielding events
- WatchCondition: pure function over event history → ConditionVerdict
- Stop-worthy verdicts integrate with dispatch's halt path (Phase 3b)

Phase 3a TDD scope: events + watch conditions in isolation. Dispatch-time
halt integration is Phase 3b alongside the TUI dashboard.
"""

from __future__ import annotations

import pytest

from axiom.compute.events import (
    KernelEvent,
    EventKind,
    WatchCondition,
    ConditionVerdict,
    LostParticleRateExceedsThreshold,
    KeffConvergedToTarget,
    TallyConvergenceSlow,
)
from axiom.compute.adapters.mock import MockKernelAdapter


# ----- KernelEvent + EventKind contracts -----


def test_event_kind_enum_includes_universal_kinds():
    """EventKind covers the universal kinds the demo spec §5.2.5 calls out."""
    assert EventKind.CYCLE_COMPLETE
    assert EventKind.TALLY_UPDATE
    assert EventKind.WARNING_EMITTED
    assert EventKind.LOST_PARTICLE
    assert EventKind.CONVERGENCE_FLAG
    assert EventKind.WALL_TIME_TICK


def test_kernel_event_carries_payload():
    """A KernelEvent has kind + timestamp_ms + payload."""
    e = KernelEvent(
        kind=EventKind.CYCLE_COMPLETE,
        timestamp_ms=1234,
        payload={"cycle": 5, "k_eff": 1.0042, "phase": "active"},
    )
    assert e.kind == EventKind.CYCLE_COMPLETE
    assert e.payload["k_eff"] == 1.0042


# ----- MockKernelAdapter event_stream -----


def test_mock_adapter_event_stream_yields_per_cycle():
    """The mock kernel yields a CYCLE_COMPLETE event per simulated cycle."""
    adapter = MockKernelAdapter()
    events = list(adapter.event_stream(
        determinism_state={"k_eff_target": 1.0042, "n_cycles": 5, "rng_seed": 1},
        kernel_options={},
    ))
    cycle_events = [e for e in events if e.kind == EventKind.CYCLE_COMPLETE]
    assert len(cycle_events) == 5
    # Last cycle should report the target k-eff
    assert cycle_events[-1].payload["k_eff"] == pytest.approx(1.0042, abs=1e-6)


def test_mock_adapter_event_stream_with_lost_particles_emits_lost_event():
    """When inject_fault=lost_particles, the stream emits LOST_PARTICLE events."""
    adapter = MockKernelAdapter()
    events = list(adapter.event_stream(
        determinism_state={"k_eff_target": 1.0, "n_cycles": 5},
        kernel_options={"inject_fault": "lost_particles"},
    ))
    lost = [e for e in events if e.kind == EventKind.LOST_PARTICLE]
    assert len(lost) >= 1


# ----- WatchCondition contracts -----


def test_lost_particle_rate_exceeds_threshold_triggers():
    """Lost particles above 1e-3 of N → stop-worthy verdict."""
    history = [
        KernelEvent(
            kind=EventKind.LOST_PARTICLE,
            timestamp_ms=1000,
            payload={"lost_particles": 8432, "n_particles": 1_000_000, "rate": 8.4e-3},
        ),
    ]
    cond = LostParticleRateExceedsThreshold(threshold=1e-3)
    verdict = cond.evaluate(history)
    assert verdict.triggered
    assert verdict.classification == "geometry error"
    assert verdict.severity == "stop_worthy"


def test_lost_particle_rate_below_threshold_does_not_trigger():
    """Lost particles below threshold → not triggered."""
    history = [
        KernelEvent(
            kind=EventKind.LOST_PARTICLE,
            timestamp_ms=1000,
            payload={"lost_particles": 100, "n_particles": 1_000_000, "rate": 1e-4},
        ),
    ]
    cond = LostParticleRateExceedsThreshold(threshold=1e-3)
    verdict = cond.evaluate(history)
    assert not verdict.triggered


def test_keff_converged_to_target_triggers_after_stable_cycles():
    """k-eff stable to within tolerance for K consecutive cycles → triggered (info: success)."""
    # Build a history of cycle_complete events with k-eff converging
    history = [
        KernelEvent(
            kind=EventKind.CYCLE_COMPLETE,
            timestamp_ms=i * 100,
            payload={"cycle": i, "k_eff": 1.0035 + 0.00001 * i, "phase": "active"},
        )
        for i in range(20)
    ]
    cond = KeffConvergedToTarget(target=1.0035, tolerance=0.0005, stable_cycles=10)
    verdict = cond.evaluate(history)
    assert verdict.triggered
    # Convergence is "info" severity (not stop-worthy by default; user opts in
    # via :auto-stop suffix on --watch flag per demo spec §4.0.4)
    assert verdict.severity == "info"
    assert "converged" in verdict.classification


def test_keff_not_converged_does_not_trigger():
    """k-eff still oscillating → not triggered."""
    history = [
        KernelEvent(
            kind=EventKind.CYCLE_COMPLETE,
            timestamp_ms=i * 100,
            payload={"cycle": i, "k_eff": 1.0035 + 0.01 * (i % 2)},  # oscillating
        )
        for i in range(20)
    ]
    cond = KeffConvergedToTarget(target=1.0035, tolerance=0.0005, stable_cycles=10)
    verdict = cond.evaluate(history)
    assert not verdict.triggered


def test_tally_convergence_slow_triggers_when_rate_below_target():
    """A tally not decreasing relative error fast enough → watch verdict (continue, not stop)."""
    history = []
    for i in range(20):
        history.append(KernelEvent(
            kind=EventKind.TALLY_UPDATE,
            timestamp_ms=i * 100,
            payload={"tally_name": "fission_pin_F12", "rel_err": 0.025, "target": 0.01},
        ))
    cond = TallyConvergenceSlow(target_rel_err=0.01, min_cycles_before_check=10)
    verdict = cond.evaluate(history)
    assert verdict.triggered
    assert verdict.severity == "watch"  # not stop-worthy; just an alert
    assert "below" in verdict.classification.lower() or "slow" in verdict.classification.lower()


def test_condition_verdict_includes_evidence():
    """A triggered verdict carries the evidence dict (numbers + event refs)."""
    history = [
        KernelEvent(
            kind=EventKind.LOST_PARTICLE,
            timestamp_ms=500,
            payload={"lost_particles": 5000, "n_particles": 1_000_000, "rate": 5e-3},
        ),
    ]
    cond = LostParticleRateExceedsThreshold(threshold=1e-3)
    verdict = cond.evaluate(history)
    assert verdict.evidence["lost_particles"] == 5000
    assert verdict.evidence["rate"] == pytest.approx(5e-3)
    assert verdict.evidence["threshold"] == pytest.approx(1e-3)
