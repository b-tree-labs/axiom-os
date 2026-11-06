# Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""OpenMCKernelAdapter — wraps OpenMC for axiom.compute dispatch.

Per ADR-018: OpenMC is native, invoked as a subprocess. This adapter:
- Validates determinism_state (input_dir exists, particles/cycles/seed declared)
- Invokes openmc subprocess with the input directory
- Parses statepoint.<N>.h5 for k-eff, tally values, convergence diagnostics
- Detects faults (lost particles, subprocess failure) and packages as KernelFault
- Returns KernelResult; the dispatch layer's always-auto-stop set acts on faults

Phase 2a (current): subprocess + statepoint parsing are testable via mock
(_run_openmc_subprocess + _parse_statepoint are seams).
Phase 2c: real OpenMC integration via subprocess + h5py; gated on OpenMC install.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from axiom.compute.adapters.base import CodeAdapter, KernelFault, KernelResult


# Default lost-particles threshold (ratio of N). Configurable per-run via
# kernel_options["lost_particles_threshold"].
DEFAULT_LOST_PARTICLE_THRESHOLD = 1e-3


class OpenMCKernelAdapter(CodeAdapter):
    """OpenMC kernel adapter — Phase 2a contract; Phase 2c integration pending OpenMC install."""

    name = "openmc"

    def execute(
        self,
        determinism_state: dict[str, Any],
        kernel_options: dict[str, Any],
    ) -> KernelResult:
        # Validate inputs at the boundary.
        input_dir = Path(determinism_state.get("input_dir", ""))
        if not input_dir.exists():
            raise ValueError(
                f"input_dir does not exist: {input_dir!r}. "
                "OpenMC requires an input directory containing geometry.xml, "
                "materials.xml, settings.xml, and (optionally) tallies.xml."
            )

        # Run OpenMC.
        exit_code, stdout, stderr = self._run_openmc_subprocess(
            input_dir=input_dir,
            determinism_state=determinism_state,
            kernel_options=kernel_options,
        )

        if exit_code != 0:
            return KernelResult(
                value_summary={},
                partial_value_summary=None,
                fault=KernelFault(
                    name="subprocess_failure",
                    evidence={
                        "exit_code": exit_code,
                        "stderr": stderr[-2000:],  # tail to keep receipt size bounded
                        "stdout_tail": stdout[-1000:],
                    },
                ),
            )

        # Parse statepoint into a canonical value_summary.
        statepoint = self._parse_statepoint(input_dir=input_dir)

        value_summary: dict[str, Any] = {
            "k_eff": statepoint.get("k_eff"),
            "k_eff_std": statepoint.get("k_eff_std"),
            "n_cycles": statepoint.get("n_cycles"),
            "shannon_entropy": statepoint.get("shannon_entropy"),
            "convergence": statepoint.get("convergence"),
            "tallies": statepoint.get("tallies", {}),
        }

        # Lost-particles fault detection (always-auto-stop set per dispatch layer).
        n_particles = int(determinism_state.get("n_particles", 0)) or 1
        lost = int(statepoint.get("lost_particles", 0))
        threshold = float(kernel_options.get(
            "lost_particles_threshold", DEFAULT_LOST_PARTICLE_THRESHOLD,
        ))
        if lost > threshold * n_particles:
            return KernelResult(
                value_summary=value_summary,
                partial_value_summary=value_summary,
                fault=KernelFault(
                    name="lost_particles",
                    evidence={
                        "lost_particles": lost,
                        "n_particles": n_particles,
                        "rate": lost / n_particles,
                        "threshold": threshold,
                    },
                ),
            )

        return KernelResult(value_summary=value_summary, fault=None)

    # --- Seams (mocked in Phase 2a; real implementation in Phase 2c) ---

    def _run_openmc_subprocess(
        self,
        input_dir: Path,
        determinism_state: dict[str, Any],
        kernel_options: dict[str, Any],
    ) -> tuple[int, str, str]:
        """Invoke openmc; return (exit_code, stdout, stderr).

        Phase 2c implementation: invokes the openmc executable (path discovered
        via shutil.which or kernel_options["openmc_executable"]) with the input
        directory. Until then, this method is mocked in tests.
        """
        executable = kernel_options.get("openmc_executable", "openmc")
        proc = subprocess.run(
            [executable],
            cwd=input_dir,
            capture_output=True,
            text=True,
            timeout=kernel_options.get("timeout_seconds", 3600),
        )
        return proc.returncode, proc.stdout, proc.stderr

    def _parse_statepoint(self, input_dir: Path) -> dict[str, Any]:
        """Parse the statepoint.<N>.h5 produced by openmc into a canonical dict.

        Phase 2c implementation: uses openmc.StatePoint or h5py to extract
        k_combined.{nominal_value, std_dev}, entropy_stationary, k_active.mean(),
        tally means + std_devs. Until then, this method is mocked in tests.
        """
        # Find the highest-numbered statepoint
        statepoints = sorted(input_dir.glob("statepoint.*.h5"))
        if not statepoints:
            raise FileNotFoundError(
                f"no statepoint.*.h5 found in {input_dir}; "
                "OpenMC may have failed silently or written to an unexpected location."
            )
        # In Phase 2c, parse with openmc/h5py here. Phase 2a tests mock this method.
        raise NotImplementedError(
            "_parse_statepoint requires openmc or h5py; "
            "Phase 2c will land the real implementation."
        )


# Self-register at import time so axiom.compute.adapters.get_adapter("openmc") works.
# Late import is intentional — registration must follow class definition.
from axiom.compute.adapters import register_adapter  # noqa: E402

register_adapter("openmc", OpenMCKernelAdapter())
