# Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Phase 3c — TUI dashboard state computation + rendering.

Per Twin Toolkit Demo Spec §9.1 (the live run dashboard).

The dashboard has two layers:
1. **State computation** (DashboardState) — pure function of (event_history,
   watch_verdicts) → renderable snapshot. Fully TDD-able.
2. **Rich rendering** — turns DashboardState into terminal output via Rich.
   Smoke-tested (does it render without crashing) but visual-correctness is
   manual review.

This file tests layer 1 exhaustively + smoke-tests layer 2.
"""

from __future__ import annotations

import pytest

from axiom.compute.events import (
    KernelEvent,
    EventKind,
    LostParticleRateExceedsThreshold,
    KeffConvergedToTarget,
)
from axiom.compute.dashboard import (
    DashboardState,
    compute_dashboard_state,
    render_dashboard_snapshot,
)


def _cycle_event(cycle: int, k_eff: float, phase: str = "active") -> KernelEvent:
    return KernelEvent(
        kind=EventKind.CYCLE_COMPLETE,
        timestamp_ms=cycle * 50,
        payload={"cycle": cycle, "k_eff": k_eff, "phase": phase, "shannon_entropy": 6.1},
    )


def _tally_event(name: str, rel_err: float, target: float = 0.01) -> KernelEvent:
    return KernelEvent(
        kind=EventKind.TALLY_UPDATE,
        timestamp_ms=1000,
        payload={"tally_name": name, "rel_err": rel_err, "target": target},
    )


# ----- DashboardState -----


def test_empty_events_produces_empty_state():
    """Zero events → empty state (no progress, no k-eff, no watches triggered)."""
    state = compute_dashboard_state(events=[], watch_conditions=[])
    assert state.cycles_completed == 0
    assert state.last_k_eff is None
    assert state.last_k_eff_std is None
    assert state.tally_count == 0
    assert state.lost_particles == 0
    assert state.triggered_watches == []


def test_cycle_count_progresses():
    """N cycle events → cycles_completed = N."""
    events = [_cycle_event(i, 1.0 + i * 0.001) for i in range(1, 11)]
    state = compute_dashboard_state(events=events, watch_conditions=[])
    assert state.cycles_completed == 10
    assert state.last_k_eff == pytest.approx(1.010, abs=1e-6)


def test_inactive_vs_active_cycle_split():
    """Phase distinction: inactive + active cycles tracked separately."""
    events = (
        [_cycle_event(i, 0.99, phase="inactive") for i in range(1, 6)] +
        [_cycle_event(i, 1.005, phase="active") for i in range(6, 11)]
    )
    state = compute_dashboard_state(events=events, watch_conditions=[])
    assert state.inactive_cycles == 5
    assert state.active_cycles == 5


def test_k_eff_trajectory_for_sparkline():
    """The state surfaces a list of recent k_eff values for sparkline rendering."""
    events = [_cycle_event(i, 1.0 + 0.0001 * i) for i in range(1, 21)]
    state = compute_dashboard_state(events=events, watch_conditions=[])
    # At least the last several values are kept
    assert len(state.k_eff_trajectory) >= 5
    # Last value is the most recent
    assert state.k_eff_trajectory[-1] == pytest.approx(1.0020, abs=1e-6)


def test_lost_particles_counted_from_events():
    """LOST_PARTICLE events accumulate into total lost_particles count."""
    events = [
        _cycle_event(1, 1.0),
        KernelEvent(
            kind=EventKind.LOST_PARTICLE,
            timestamp_ms=100,
            payload={"lost_particles": 1234, "n_particles": 100000, "rate": 0.012},
        ),
    ]
    state = compute_dashboard_state(events=events, watch_conditions=[])
    assert state.lost_particles == 1234


def test_tally_state_computed_from_tally_updates():
    """TALLY_UPDATE events populate per-tally relative error state."""
    events = [
        _tally_event("pin_C7", rel_err=0.008),
        _tally_event("pin_F12", rel_err=0.024),
        _tally_event("pin_C7", rel_err=0.007),  # later update wins
    ]
    state = compute_dashboard_state(events=events, watch_conditions=[])
    assert state.tally_count == 2
    assert state.tallies["pin_C7"] == pytest.approx(0.007)
    assert state.tallies["pin_F12"] == pytest.approx(0.024)


def test_watch_condition_verdicts_surface_in_state():
    """Triggered watch conditions populate triggered_watches in the state."""
    events = [
        KernelEvent(
            kind=EventKind.LOST_PARTICLE,
            timestamp_ms=100,
            payload={"lost_particles": 8432, "n_particles": 1_000_000, "rate": 8.4e-3},
        ),
    ]
    cond = LostParticleRateExceedsThreshold(threshold=1e-3)
    state = compute_dashboard_state(events=events, watch_conditions=[cond])
    assert len(state.triggered_watches) == 1
    name, verdict = state.triggered_watches[0]
    assert name == "lost_particles_rate_exceeds_threshold"
    assert verdict.triggered is True


def test_untriggered_watch_does_not_appear():
    """Watch conditions that don't fire are NOT in triggered_watches."""
    events = [_cycle_event(i, 1.005 + 0.01 * (i % 2)) for i in range(1, 11)]  # oscillating
    cond = KeffConvergedToTarget(target=1.005, tolerance=0.001, stable_cycles=5)
    state = compute_dashboard_state(events=events, watch_conditions=[cond])
    assert state.triggered_watches == []


# ----- render_dashboard_snapshot smoke tests -----


def test_renderer_produces_string_for_empty_state():
    """Renderer handles empty state without crashing."""
    state = compute_dashboard_state(events=[], watch_conditions=[])
    output = render_dashboard_snapshot(state, model_id="test", peer_id="laptop", kernel="mock")
    assert isinstance(output, str)
    # Empty state should still mention the run identity
    assert "mock" in output or "test" in output


def test_renderer_includes_k_eff_when_present():
    """When cycle events have produced a k-eff, the renderer surfaces it."""
    events = [_cycle_event(i, 1.0042 + 0.0001 * i) for i in range(1, 11)]
    state = compute_dashboard_state(events=events, watch_conditions=[])
    output = render_dashboard_snapshot(state, model_id="m", peer_id="p", kernel="k")
    # Should include k-eff numerical value somewhere
    assert "k-eff" in output.lower() or "k_eff" in output.lower() or "1.005" in output


def test_renderer_surfaces_triggered_watch():
    """Triggered watch conditions appear in the rendered output."""
    events = [
        KernelEvent(
            kind=EventKind.LOST_PARTICLE,
            timestamp_ms=100,
            payload={"lost_particles": 8432, "n_particles": 1_000_000, "rate": 8.4e-3},
        ),
    ]
    cond = LostParticleRateExceedsThreshold(threshold=1e-3)
    state = compute_dashboard_state(events=events, watch_conditions=[cond])
    output = render_dashboard_snapshot(state, model_id="m", peer_id="p", kernel="k")
    # Should mention the geometry-error classification or lost particles
    assert "lost" in output.lower() or "geometry" in output.lower()
