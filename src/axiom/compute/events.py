# Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Live event stream + watch condition primitives — Seam G of the demo spec.

Per Twin Toolkit Demo Spec §5.2.5:

- KernelEvent: structured records emitted during execution
- EventKind: enum of universal event kinds
- WatchCondition: pure function over event history → ConditionVerdict
- ConditionVerdict: triggered? + severity + classification + suggested action + evidence

Phase 3a ships the data shapes + three reference watch conditions (lost
particles, k-eff converged, tally convergence slow). Phase 3b integrates with
dispatch's halt path so stop-worthy verdicts auto-emit halted-receipts.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class EventKind(str, Enum):
    """Universal kernel-event kinds. Per-code adapters can extend with custom kinds."""

    CYCLE_COMPLETE = "cycle_complete"          # Monte Carlo cycle (active or inactive) finished
    TALLY_UPDATE = "tally_update"              # tally relative-error update
    WARNING_EMITTED = "warning_emitted"         # kernel emitted a warning line
    LOST_PARTICLE = "lost_particle"            # MC geometry-error indicator
    CONVERGENCE_FLAG = "convergence_flag"      # source converged / not
    ITERATION_RESIDUAL = "iteration_residual"  # MPACT outer/inner residual
    TIME_STEP_COMPLETE = "time_step_complete"  # SAM/RELAP time step done
    WALL_TIME_TICK = "wall_time_tick"          # periodic ETA refresh
    KERNEL_START = "kernel_start"              # the kernel began execution
    KERNEL_END = "kernel_end"                  # the kernel finished cleanly


@dataclass(frozen=True)
class KernelEvent:
    """One structured event from a running kernel."""

    kind: EventKind
    timestamp_ms: int
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ConditionVerdict:
    """The result of evaluating a WatchCondition against event history."""

    triggered: bool
    classification: str  # human-readable: "geometry error", "convergence success", …
    severity: str  # "info" | "watch" | "stop_worthy"
    suggested_action: str  # "continue" | "investigate" | "stop"
    evidence: dict[str, Any] = field(default_factory=dict)
    auto_stop: bool = False  # True only if user opted in or if condition is in always-auto-stop set


class WatchCondition(ABC):
    """Pure function over event history → ConditionVerdict.

    Per Twin Toolkit Demo Spec §5.2.5:
    - name: stable identifier
    - severity: declared severity class
    - auto_stop_eligible: whether this condition can participate in --watch :auto-stop
    - evaluate(history): returns ConditionVerdict
    """

    name: str
    severity: str  # "info" | "watch" | "stop_worthy"
    classification: str  # default classification when triggered
    auto_stop_eligible: bool

    @abstractmethod
    def evaluate(self, history: list[KernelEvent]) -> ConditionVerdict:
        ...


# ----- Reference watch conditions (universal Monte Carlo set) -----


class LostParticleRateExceedsThreshold(WatchCondition):
    """Trigger when the cumulative lost-particle rate exceeds threshold.

    Stop-worthy by construction — geometry errors invalidate results, so
    every additional second of compute is wasted. Member of the always-auto-stop
    set in axiom.compute (per ADR-018 + Twin Toolkit Demo Spec §4.0).
    """

    name = "lost_particles_rate_exceeds_threshold"
    severity = "stop_worthy"
    classification = "geometry error"
    auto_stop_eligible = True

    def __init__(self, threshold: float = 1e-3) -> None:
        self.threshold = threshold

    def evaluate(self, history: list[KernelEvent]) -> ConditionVerdict:
        for event in reversed(history):
            if event.kind == EventKind.LOST_PARTICLE:
                rate = float(event.payload.get("rate", 0))
                if rate > self.threshold:
                    return ConditionVerdict(
                        triggered=True,
                        classification=self.classification,
                        severity=self.severity,
                        suggested_action="stop",
                        evidence={
                            "lost_particles": event.payload.get("lost_particles"),
                            "n_particles": event.payload.get("n_particles"),
                            "rate": rate,
                            "threshold": self.threshold,
                        },
                        auto_stop=True,  # always-auto-stop set member
                    )
        return ConditionVerdict(
            triggered=False,
            classification=self.classification,
            severity=self.severity,
            suggested_action="continue",
        )


class KeffConvergedToTarget(WatchCondition):
    """Trigger when k-eff has been within tolerance of target for K consecutive cycles.

    'info' severity — convergence is success, not failure. User opts in to
    auto-stop via --watch keff_converged_to_target:auto-stop on the CLI.
    """

    name = "keff_converged_to_target"
    severity = "info"
    classification = "k-eff converged to target"
    auto_stop_eligible = True

    def __init__(self, target: float, tolerance: float = 5e-4, stable_cycles: int = 10) -> None:
        self.target = target
        self.tolerance = tolerance
        self.stable_cycles = stable_cycles

    def evaluate(self, history: list[KernelEvent]) -> ConditionVerdict:
        cycle_events = [e for e in history if e.kind == EventKind.CYCLE_COMPLETE]
        if len(cycle_events) < self.stable_cycles:
            return ConditionVerdict(
                triggered=False,
                classification=self.classification,
                severity=self.severity,
                suggested_action="continue",
            )
        recent = cycle_events[-self.stable_cycles:]
        all_within = all(
            abs(float(e.payload.get("k_eff", 0)) - self.target) <= self.tolerance
            for e in recent
        )
        if all_within:
            last = recent[-1].payload.get("k_eff")
            return ConditionVerdict(
                triggered=True,
                classification=self.classification,
                severity=self.severity,
                suggested_action="stop",  # opt-in via --watch :auto-stop
                evidence={
                    "target": self.target,
                    "tolerance": self.tolerance,
                    "stable_cycles": self.stable_cycles,
                    "last_k_eff": last,
                },
            )
        return ConditionVerdict(
            triggered=False,
            classification=self.classification,
            severity=self.severity,
            suggested_action="continue",
        )


class TallyConvergenceSlow(WatchCondition):
    """Trigger when a tally's relative error is not approaching its target fast enough.

    'watch' severity — not stop-worthy; alerts the user that this tally may
    not hit its target within the planned cycle budget. Useful for "this run
    will produce noisy tally X" warnings.
    """

    name = "tally_convergence_slow"
    severity = "watch"
    classification = "tally below convergence rate"
    auto_stop_eligible = False  # never auto-stops

    def __init__(self, target_rel_err: float = 0.01, min_cycles_before_check: int = 10) -> None:
        self.target_rel_err = target_rel_err
        self.min_cycles_before_check = min_cycles_before_check

    def evaluate(self, history: list[KernelEvent]) -> ConditionVerdict:
        tally_events = [e for e in history if e.kind == EventKind.TALLY_UPDATE]
        if len(tally_events) < self.min_cycles_before_check:
            return ConditionVerdict(
                triggered=False,
                classification=self.classification,
                severity=self.severity,
                suggested_action="continue",
            )
        # Group by tally_name; flag any tally whose latest rel_err is > 2× target
        latest_per_tally: dict[str, KernelEvent] = {}
        for event in tally_events:
            name = event.payload.get("tally_name", "<unknown>")
            latest_per_tally[name] = event
        slow_tallies = [
            (name, event) for name, event in latest_per_tally.items()
            if float(event.payload.get("rel_err", 0)) > 2 * self.target_rel_err
        ]
        if slow_tallies:
            name, event = slow_tallies[0]
            return ConditionVerdict(
                triggered=True,
                classification=f"tally below convergence rate: {name}",
                severity=self.severity,
                suggested_action="continue",  # likely converges with more cycles
                evidence={
                    "tally_name": name,
                    "rel_err": event.payload.get("rel_err"),
                    "target_rel_err": self.target_rel_err,
                    "n_tally_updates": len(tally_events),
                },
            )
        return ConditionVerdict(
            triggered=False,
            classification=self.classification,
            severity=self.severity,
            suggested_action="continue",
        )
