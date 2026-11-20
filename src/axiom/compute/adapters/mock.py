# Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""MockKernelAdapter — deterministic Python kernel for TDD.

Per ADR-018 (revised 2026-05-04): the mock kernel is plain Python, not WASM.
Determinism is by construction (pure functions over the determinism_state dict).

Behaviors driven by determinism_state:
- "k_eff_target": float — what k_eff the mock will report
- "n_cycles": int — how many synthetic cycles to "simulate"
- "rng_seed": int — included in canonical message; varies content address even
  though the mock's value_summary doesn't depend on the seed.

Behaviors driven by kernel_options:
- "inject_fault": "lost_particles" | "cfl_violation" | "negative_density" | None
"""

from __future__ import annotations

from typing import Any, Iterator

from axiom.compute.adapters.base import CodeAdapter, KernelFault, KernelResult
from axiom.compute.events import EventKind, KernelEvent


class MockKernelAdapter(CodeAdapter):
    name = "mock"

    def event_stream(
        self,
        determinism_state: dict[str, Any],
        kernel_options: dict[str, Any],
    ) -> Iterator[KernelEvent]:
        """Yield synthetic events for a simulated run.

        For each cycle in n_cycles, yields one CYCLE_COMPLETE event with k_eff
        converging linearly toward k_eff_target. If inject_fault=lost_particles,
        yields a LOST_PARTICLE event partway through the run.
        """
        n_cycles = int(determinism_state.get("n_cycles", 50))
        k_eff_target = float(determinism_state.get("k_eff_target", 1.0))
        injected = kernel_options.get("inject_fault")

        # Synthetic timeline: 50ms per cycle.
        for cycle in range(1, n_cycles + 1):
            # Linear convergence from 0.95 → k_eff_target across the run
            progress = cycle / n_cycles
            k_eff = 0.95 + (k_eff_target - 0.95) * progress
            # Last cycle reports the target exactly
            if cycle == n_cycles:
                k_eff = k_eff_target
            yield KernelEvent(
                kind=EventKind.CYCLE_COMPLETE,
                timestamp_ms=cycle * 50,
                payload={
                    "cycle": cycle,
                    "k_eff": k_eff,
                    "phase": "active" if cycle > n_cycles // 4 else "inactive",
                    "shannon_entropy": 6.0 + 0.1 * progress,
                },
            )
            # Fault injection at ~halfway through the run
            if injected == "lost_particles" and cycle == max(1, n_cycles // 2):
                yield KernelEvent(
                    kind=EventKind.LOST_PARTICLE,
                    timestamp_ms=cycle * 50 + 10,
                    payload={
                        "lost_particles": 8432,
                        "n_particles": 1_000_000,
                        "rate": 8.432e-3,
                    },
                )

    def execute(
        self,
        determinism_state: dict[str, Any],
        kernel_options: dict[str, Any],
    ) -> KernelResult:
        k_eff_target: float = float(determinism_state.get("k_eff_target", 1.0))
        n_cycles: int = int(determinism_state.get("n_cycles", 50))

        # The "physics": deterministic value_summary entirely from determinism_state.
        value_summary: dict[str, Any] = {
            "k_eff": k_eff_target,
            "k_eff_std": 0.00012,  # canned plausible standard deviation
            "n_cycles": n_cycles,
            "shannon_entropy": 6.13,  # canned plausible value
            "convergence": "stationary",
        }

        # Optional fault injection (drives the always-auto-stop test path).
        fault: KernelFault | None = None
        injected = kernel_options.get("inject_fault")
        if injected == "lost_particles":
            fault = KernelFault(
                name="lost_particles",
                evidence={
                    "lost_particles": 8432,
                    "histories_run": 5_234_891,
                    "histories_planned": 10_000_000,
                    "rate": 1.61e-3,
                    "threshold": 1.0e-3,
                    "first_lost_event_index": 247,
                },
            )
        elif injected == "cfl_violation":
            fault = KernelFault(
                name="cfl_violation",
                evidence={"max_cfl": 1.42, "threshold": 1.0},
            )
        elif injected == "negative_density":
            fault = KernelFault(
                name="negative_density",
                evidence={"min_density": -1.2e-4},
            )

        return KernelResult(
            value_summary=value_summary,
            partial_value_summary=value_summary if fault else None,
            fault=fault,
        )
