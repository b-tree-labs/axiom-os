# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""BaseAgent — self-managed runtime + health contract (#30).

Per project_auto_mcp_and_agent_self_health memory: every agent in
Axiom (AXI, SCAN, CURIO, TIDY, PRESS, TRIAGE, plus extension-supplied
agents like CHALKE) inherits a uniform health contract so operators
have one lens on the fleet and TIDY has one surface to monitor.

The base class is deliberately minimal — it defines the shape, records
vitals, provides a pluggable restart policy. Concrete agents mix in
their domain logic + call the base methods at appropriate points.

Health model:
- vitals (uptime, last_activity, error_count, restart_count, queue
  depths, latency samples)
- status: HEALTHY | DEGRADED | RESTARTING | FAILED
- health endpoint (serves JSON for TIDY's fleet watcher)
- bounded-retry restart on exceptions

Federation-aware: agent status can be published as a MemoryFragment
(episodic) via composition so peer nodes observe degradation across
the federation (future integration hook).
"""

from __future__ import annotations

import statistics
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class AgentStatus(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    RESTARTING = "restarting"
    FAILED = "failed"


@dataclass
class AgentVitals:
    """Running health-metric counters for an agent."""

    started_at: float = field(default_factory=time.monotonic)
    last_activity: float = field(default_factory=time.monotonic)
    error_count: int = 0
    restart_count: int = 0
    queue_depths: dict[str, int] = field(default_factory=dict)
    recent_latency_ms: list[float] = field(default_factory=list)

    # Tunable caps
    max_latency_samples: int = 64

    def touch(self) -> None:
        self.last_activity = time.monotonic()

    def record_latency(self, ms: float) -> None:
        self.recent_latency_ms.append(ms)
        if len(self.recent_latency_ms) > self.max_latency_samples:
            self.recent_latency_ms = self.recent_latency_ms[-self.max_latency_samples:]

    def record_error(self) -> None:
        self.error_count += 1
        self.last_activity = time.monotonic()

    def set_queue_depth(self, queue_name: str, depth: int) -> None:
        self.queue_depths[queue_name] = depth

    def uptime_seconds(self) -> float:
        return time.monotonic() - self.started_at

    def seconds_since_last_activity(self) -> float:
        return time.monotonic() - self.last_activity

    def mean_latency_ms(self) -> float | None:
        if not self.recent_latency_ms:
            return None
        return statistics.fmean(self.recent_latency_ms)

    def to_dict(self) -> dict:
        return {
            "uptime_seconds": self.uptime_seconds(),
            "last_activity_age_seconds": self.seconds_since_last_activity(),
            "error_count": self.error_count,
            "restart_count": self.restart_count,
            "queue_depths": dict(self.queue_depths),
            "mean_latency_ms": self.mean_latency_ms(),
            "latency_samples": len(self.recent_latency_ms),
        }


# ---------------------------------------------------------------------------
# BaseAgent
# ---------------------------------------------------------------------------


@dataclass
class BaseAgent:
    """Base health contract for Axiom agents.

    Subclasses add domain methods; call self.vitals.touch() at the
    start of each unit of work and self.vitals.record_latency() /
    record_error() appropriately.
    """

    agent_id: str
    max_restarts: int = 5
    idle_degraded_seconds: float = 600.0  # 10 min
    error_rate_degraded: int = 10          # errors before DEGRADED

    # Internal
    vitals: AgentVitals = field(default_factory=AgentVitals)
    _status: AgentStatus = AgentStatus.HEALTHY

    # RACI ledger — every agent inherits the propose/approve/deny machinery
    # so consumers can ask "should I run automation X?" with a single API
    # without each agent reimplementing the state machine.
    # See `axiom.agents.raci` for the lifecycle.
    raci: Any = field(default=None)

    def __post_init__(self) -> None:
        if self.raci is None:
            from axiom.agents.raci import RACILedger
            self.raci = RACILedger()

    # ------- RACI proposal API --------------------------------------------

    def propose_action(self, action_class: str) -> Any:
        """Ask the ledger whether to run the named automation.

        Returns a ``ProposalDecision`` (AUTO / ASK / SKIP). Wraps
        ``self.raci.propose`` so subclasses can override the decision
        path without leaking the ledger surface to callers.
        """
        return self.raci.propose(action_class)

    # ------- Health derivation ---------------------------------------------

    @property
    def status(self) -> AgentStatus:
        """Derive status from vitals. Cache as _status for read consistency."""
        if self._status in (AgentStatus.RESTARTING, AgentStatus.FAILED):
            return self._status
        if self.vitals.error_count >= self.error_rate_degraded:
            return AgentStatus.DEGRADED
        if self.vitals.seconds_since_last_activity() > self.idle_degraded_seconds:
            return AgentStatus.DEGRADED
        return AgentStatus.HEALTHY

    def health(self) -> dict:
        """Return the agent's health payload (serializable JSON)."""
        return {
            "agent_id": self.agent_id,
            "status": self.status.value,
            "vitals": self.vitals.to_dict(),
        }

    # ------- Restart lifecycle --------------------------------------------

    def restart(self) -> bool:
        """Trigger a bounded-retry restart.

        Returns True iff the restart was performed. Returns False when
        max_restarts is exhausted (agent transitions to FAILED —
        operator intervention required).
        """
        if self.vitals.restart_count >= self.max_restarts:
            self._status = AgentStatus.FAILED
            return False
        self._status = AgentStatus.RESTARTING
        self.vitals.restart_count += 1
        self._on_restart()
        self._status = AgentStatus.HEALTHY
        self.vitals.error_count = 0  # reset error window post-restart
        self.vitals.touch()
        return True

    def _on_restart(self) -> None:
        """Override in subclasses to clear per-run state, re-connect
        resources, etc. Default: no-op."""
        pass

    # ------- Work wrapper --------------------------------------------------

    def run_step(self, work: Callable[[], Any]) -> Any:
        """Invoke a unit of work with latency tracking + error handling.

        Captures timing + errors; bubbles the exception after recording.
        Auto-restarts if error_count exceeds threshold and max_restarts
        hasn't been exhausted.
        """
        self.vitals.touch()
        t0 = time.monotonic()
        try:
            result = work()
            self.vitals.record_latency((time.monotonic() - t0) * 1000)
            return result
        except Exception:
            self.vitals.record_latency((time.monotonic() - t0) * 1000)
            self.vitals.record_error()
            if self.vitals.error_count >= self.error_rate_degraded:
                self.restart()
            raise
