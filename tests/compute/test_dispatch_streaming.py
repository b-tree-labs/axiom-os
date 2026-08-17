# Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Phase 3b — dispatch integration with the event stream + watch condition halt path.

Per Twin Toolkit Demo Spec §5.2.5 (Seam G):
- A new dispatch entry point `dispatch_streaming` consumes adapter.event_stream
  in lockstep with execution, evaluates registered WatchConditions on each event,
  and halts execution when any auto-stop verdict triggers.
- The result is a HaltedDispatchResult whose halt_condition derives from the
  triggered WatchCondition (rather than from a kernel-side fault).

Note on architecture: the synchronous `dispatch()` from Phase 0 stays as the
fast path for kernels that don't stream events (everything works as before).
`dispatch_streaming()` is the streaming path; it reuses the same signing,
receipt-emission, and halt-receipt machinery.
"""

from __future__ import annotations

import pytest

from axiom.compute import (
    DispatchSpec,
    DispatchResult,
    HaltedDispatchResult,
)
from axiom.compute.dispatch import dispatch_streaming, verify_signature
from axiom.compute.events import (
    LostParticleRateExceedsThreshold,
    KeffConvergedToTarget,
)


@pytest.fixture
def basic_spec():
    return DispatchSpec(
        model_id="streaming-test",
        composition_hash="sha256:" + "0" * 64,
        kernel="mock",
        peer_id="laptop",
        determinism_class="D-bit",
        determinism_state={"k_eff_target": 1.0042, "n_cycles": 20, "rng_seed": 42},
    )


def test_streaming_completes_normally_when_no_watch_triggers(basic_spec):
    """Without watch conditions, streaming dispatch behaves like sync dispatch."""
    result = dispatch_streaming(basic_spec, watch_conditions=[])
    assert isinstance(result, DispatchResult)
    assert not result.halted
    assert result.value_summary["k_eff"] == pytest.approx(1.0042, abs=1e-6)
    assert verify_signature(result)


def test_streaming_halts_when_lost_particles_watch_triggers(basic_spec):
    """LostParticleRateExceeds (auto_stop=True) halts mid-stream → halted-receipt."""
    spec_faulty = DispatchSpec(
        model_id=basic_spec.model_id,
        composition_hash=basic_spec.composition_hash,
        kernel=basic_spec.kernel,
        peer_id=basic_spec.peer_id,
        determinism_class=basic_spec.determinism_class,
        determinism_state=basic_spec.determinism_state,
        kernel_options={"inject_fault": "lost_particles"},
    )
    cond = LostParticleRateExceedsThreshold(threshold=1e-3)
    result = dispatch_streaming(spec_faulty, watch_conditions=[cond])

    assert isinstance(result, HaltedDispatchResult)
    assert result.halted
    assert result.uri.startswith("axiom://compute/halt:sha256:")
    assert result.halt_condition.name == "lost_particles_rate_exceeds_threshold"
    assert result.halt_condition.classification == "geometry error"
    # Signature verifies on halted receipt too
    assert verify_signature(result)


def test_streaming_does_not_halt_on_info_severity_unless_user_opts_in(basic_spec):
    """KeffConvergedToTarget is severity=info; default is don't-stop, just alert."""
    cond = KeffConvergedToTarget(target=1.0042, tolerance=0.001, stable_cycles=3)
    result = dispatch_streaming(basic_spec, watch_conditions=[cond])
    # Even though convergence triggers, severity=info doesn't auto-stop by default.
    assert not result.halted


def test_streaming_halts_on_info_when_user_passes_auto_stop_set(basic_spec):
    """User can opt in: pass condition name in user_auto_stop set → it triggers halt.

    Tolerance loosened to match the mock kernel's linear ramp from 0.95 → target
    across n_cycles cycles; only the final cycles approach the target closely.
    With tolerance=0.06 + stable_cycles=3 over the basic_spec's n_cycles=20,
    the final 3 cycles all sit within 0.06 of target.
    """
    cond = KeffConvergedToTarget(target=1.0042, tolerance=0.06, stable_cycles=3)
    result = dispatch_streaming(
        basic_spec,
        watch_conditions=[cond],
        user_auto_stop={"keff_converged_to_target"},
    )
    assert result.halted
    assert result.halt_condition.name == "keff_converged_to_target"


def test_streaming_no_auto_stop_override_lets_run_finish_despite_fault(basic_spec):
    """spec.no_auto_stop=True suppresses always-auto-stop set; run completes."""
    spec_faulty = DispatchSpec(
        model_id=basic_spec.model_id,
        composition_hash=basic_spec.composition_hash,
        kernel=basic_spec.kernel,
        peer_id=basic_spec.peer_id,
        determinism_class=basic_spec.determinism_class,
        determinism_state=basic_spec.determinism_state,
        kernel_options={"inject_fault": "lost_particles"},
        no_auto_stop=True,
    )
    cond = LostParticleRateExceedsThreshold(threshold=1e-3)
    result = dispatch_streaming(spec_faulty, watch_conditions=[cond])
    # Even though the watch triggered, no_auto_stop suppresses the halt.
    assert not result.halted


def test_streaming_records_event_count_in_receipt(basic_spec):
    """The streaming receipt records how many events were observed (light provenance)."""
    result = dispatch_streaming(basic_spec, watch_conditions=[])
    # value_summary picks up the event_count from the stream
    assert "event_count" in result.value_summary
    # Mock adapter yields 1 event per cycle (n_cycles=20 → 20 events)
    assert result.value_summary["event_count"] == 20
