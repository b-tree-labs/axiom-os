# Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Phase 2a — OpenMCKernelAdapter contract tests (mocked subprocess).

Per Phase 2 plan in twin-os-build-march.md and ADR-018:
- OpenMC is the first real physics-code adapter.
- It runs openmc as a native subprocess (no WASM); per ADR-018, physics = native.
- Phase 2a TDD against mocked subprocess + statepoint output; Phase 2c integrates
  with a real OpenMC install (separate venv or Docker).

The adapter's contract:
- execute(determinism_state, kernel_options) → KernelResult
- value_summary contains: k_eff, k_eff_std, n_cycles, shannon_entropy, tallies
- determinism_state declares: input_dir (path), n_particles, n_active_cycles,
  n_inactive_cycles, rng_seed, xs_library
- Faults detected: lost_particles (always-auto-stop), tally_relative_error_growing
- Implementation deferred: kernel_options can declare openmc_executable path
  (default: discover via shutil.which), or use the registered "docker" runner.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from axiom.compute.adapters.openmc import OpenMCKernelAdapter
from axiom.compute.adapters.base import KernelResult, KernelFault


@pytest.fixture
def adapter():
    return OpenMCKernelAdapter()


@pytest.fixture
def basic_state(tmp_path):
    """A minimal determinism_state for testing — input_dir present, no real XML needed."""
    input_dir = tmp_path / "openmc_input"
    input_dir.mkdir()
    # Phase 2a: empty dir is fine since we mock the subprocess
    return {
        "input_dir": str(input_dir),
        "n_particles": 100000,
        "n_active_cycles": 50,
        "n_inactive_cycles": 20,
        "rng_seed": 42,
        "xs_library": "ENDF/B-VIII.0",
    }


def test_adapter_name():
    """The adapter registers under the 'openmc' kernel name."""
    assert OpenMCKernelAdapter.name == "openmc"


def test_adapter_returns_kernel_result_on_success(adapter, basic_state, tmp_path):
    """A successful subprocess + statepoint parse returns a KernelResult."""
    fake_statepoint = {
        "k_eff": 1.00342,
        "k_eff_std": 0.00012,
        "n_cycles": 50,
        "shannon_entropy": 6.13,
        "convergence": "stationary",
        "tallies": {},
        "lost_particles": 0,
    }
    with patch.object(OpenMCKernelAdapter, "_run_openmc_subprocess") as run, \
         patch.object(OpenMCKernelAdapter, "_parse_statepoint") as parse:
        run.return_value = (0, "", "")  # exit=0, stdout, stderr
        parse.return_value = fake_statepoint

        result = adapter.execute(basic_state, kernel_options={})

    assert isinstance(result, KernelResult)
    assert result.fault is None
    assert result.value_summary["k_eff"] == pytest.approx(1.00342)
    assert result.value_summary["k_eff_std"] == pytest.approx(0.00012)
    assert result.value_summary["shannon_entropy"] == pytest.approx(6.13)


def test_adapter_detects_lost_particles_fault(adapter, basic_state):
    """Subprocess output indicating lost particles → KernelFault."""
    statepoint_with_lost = {
        "k_eff": 1.0,
        "k_eff_std": 0.001,
        "n_cycles": 50,
        "shannon_entropy": 6.0,
        "convergence": "stationary",
        "tallies": {},
        "lost_particles": 8432,  # exceeds default threshold (configurable)
    }
    with patch.object(OpenMCKernelAdapter, "_run_openmc_subprocess") as run, \
         patch.object(OpenMCKernelAdapter, "_parse_statepoint") as parse:
        run.return_value = (0, "", "")
        parse.return_value = statepoint_with_lost

        result = adapter.execute(basic_state, kernel_options={})

    assert result.fault is not None
    assert result.fault.name == "lost_particles"
    assert result.fault.evidence["lost_particles"] == 8432
    # The dispatch layer's always-auto-stop set will trigger on this fault.


def test_adapter_subprocess_failure_returns_kernel_result_with_no_summary(adapter, basic_state):
    """Non-zero exit → KernelResult with empty summary + fault declaring exit."""
    with patch.object(OpenMCKernelAdapter, "_run_openmc_subprocess") as run:
        run.return_value = (1, "", "openmc: error: cannot read geometry.xml")

        result = adapter.execute(basic_state, kernel_options={})

    assert result.fault is not None
    assert result.fault.name == "subprocess_failure"
    assert "cannot read geometry.xml" in result.fault.evidence.get("stderr", "")


def test_adapter_missing_input_dir_raises(adapter):
    """input_dir must be a path that exists; missing → ValueError before subprocess."""
    state = {
        "input_dir": "/nonexistent/path",
        "n_particles": 1000,
        "n_active_cycles": 10,
        "n_inactive_cycles": 5,
        "rng_seed": 1,
        "xs_library": "ENDF/B-VIII.0",
    }
    with pytest.raises(ValueError, match="input_dir does not exist"):
        adapter.execute(state, kernel_options={})


def test_adapter_registers_with_axiom_compute_registry():
    """Importing axiom.compute.adapters.openmc registers the adapter."""
    from axiom.compute.adapters import get_adapter
    a = get_adapter("openmc")
    assert isinstance(a, OpenMCKernelAdapter)
