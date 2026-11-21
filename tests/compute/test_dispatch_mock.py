# Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Phase 0 seed tests — mock kernel dispatch, the foundation of the twin toolkit.

Per the domain consumer's docs/working/twin-os-build-march.md and ADR-016, ADR-017, ADR-018:

- The atomic primitive is `dispatch(spec)` returning a signed compute receipt.
- The mock kernel is a pure-Python deterministic kernel used for TDD; no WASM, no MPI.
- Identical determinism state → identical content addresses (D-bit equivalence).
- Watch conditions can fire mid-run; the always-auto-stop set produces halted-receipts.
- Receipts are signed; signatures verify against the federation directory.

These tests are the canonical entry point; everything else builds outward from here.
"""

from __future__ import annotations

import pytest

from axiom.compute import (
    DispatchSpec,
    DispatchResult,
    HaltedDispatchResult,
    dispatch,
    verify_signature,
)


@pytest.fixture
def basic_spec():
    """A canonical spec for the mock kernel: deterministic, reproducible."""
    return DispatchSpec(
        model_id="mock-pin-cell-test-1",
        composition_hash="sha256:deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
        kernel="mock",
        peer_id="laptop",
        determinism_class="D-bit",
        determinism_state={
            "k_eff_target": 1.0042,
            "n_cycles": 50,
            "rng_seed": 42,
        },
    )


def test_mock_dispatch_emits_signed_receipt(basic_spec):
    """The atomic primitive: dispatch(spec) returns a signed receipt with the value summary."""
    result = dispatch(basic_spec)

    assert isinstance(result, DispatchResult)
    assert not result.halted
    assert result.value_summary["k_eff"] == pytest.approx(1.0042, abs=1e-6)
    assert result.signature_b64
    assert result.signing_pubkey_b64
    assert result.executing_peer_id == "laptop"
    assert result.uri.startswith("axiom://compute/sha256:")


def test_mock_dispatch_signature_verifies(basic_spec):
    """The signature on the receipt verifies against the originating peer's pubkey."""
    result = dispatch(basic_spec)
    assert verify_signature(result) is True


def test_mock_dispatch_d_bit_equivalence(basic_spec):
    """Same determinism state → same content address (the D-bit guarantee)."""
    r1 = dispatch(basic_spec)
    r2 = dispatch(basic_spec)
    assert r1.content_address == r2.content_address
    assert r1.uri == r2.uri


def test_mock_dispatch_different_seed_different_content_address(basic_spec):
    """Changing the RNG seed changes the content address (sanity check on hash)."""
    spec_alt_seed = DispatchSpec(
        model_id=basic_spec.model_id,
        composition_hash=basic_spec.composition_hash,
        kernel=basic_spec.kernel,
        peer_id=basic_spec.peer_id,
        determinism_class=basic_spec.determinism_class,
        determinism_state={**basic_spec.determinism_state, "rng_seed": 43},
    )
    r1 = dispatch(basic_spec)
    r2 = dispatch(spec_alt_seed)
    assert r1.content_address != r2.content_address


def test_mock_dispatch_with_lost_particles_auto_stops(basic_spec):
    """Always-auto-stop set: lost particles trigger a halted-receipt; partial value summary."""
    spec_faulty = DispatchSpec(
        model_id=basic_spec.model_id,
        composition_hash=basic_spec.composition_hash,
        kernel=basic_spec.kernel,
        peer_id=basic_spec.peer_id,
        determinism_class=basic_spec.determinism_class,
        determinism_state=basic_spec.determinism_state,
        kernel_options={"inject_fault": "lost_particles"},
    )
    result = dispatch(spec_faulty)

    assert isinstance(result, HaltedDispatchResult)
    assert result.halted is True
    assert result.uri.startswith("axiom://compute/halt:sha256:")
    assert result.halt_condition.name == "lost_particles_rate_exceeds_threshold"
    assert result.halt_condition.classification == "geometry error"
    assert result.value_summary_partial is not None


def test_mock_dispatch_no_auto_stop_override(basic_spec):
    """--no-auto-stop suppresses always-auto-stop; the run completes with a faulty receipt."""
    spec_faulty_no_stop = DispatchSpec(
        model_id=basic_spec.model_id,
        composition_hash=basic_spec.composition_hash,
        kernel=basic_spec.kernel,
        peer_id=basic_spec.peer_id,
        determinism_class=basic_spec.determinism_class,
        determinism_state=basic_spec.determinism_state,
        kernel_options={"inject_fault": "lost_particles"},
        no_auto_stop=True,
    )
    result = dispatch(spec_faulty_no_stop)

    # Run completes (NOT halted), but the receipt records the fault that occurred.
    assert not result.halted
    assert "warnings" in result.value_summary
    assert any("lost_particles" in w for w in result.value_summary["warnings"])
