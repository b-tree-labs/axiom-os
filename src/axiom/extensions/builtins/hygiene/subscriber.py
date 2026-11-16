# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""TIDY event observers — manifest-discovered via AEOS hook declarations.

Per spec-hooks.md §7 + §9, these handlers are wired by the platform's
`HookRegistry` from the extension's `[[extension.provides]] kind = "hook"`
blocks; there is no boot-time `bus.subscribe()` ceremony.

Handlers publish chained events through the process-default `EventBus`
(`axiom.infra.bus.get_default_eventbus()`).

Circuit breakers (rate limit + per-fingerprint cooldown) prevent the
LLM-diagnosis loop from running away on repeated signals.
"""

from __future__ import annotations

import time
from typing import Any

from axiom.infra.bus import get_default_eventbus

# --- Circuit breaker constants ---

MAX_DIAGNOSES_PER_HOUR = 3
COOLDOWN_SECONDS = 300  # 5 min between attempts on same signal

# Module-level state — single-process; thread-safety via the bus's lock.
_recent_signals: dict[str, float] = {}  # fingerprint -> timestamp
_hourly_calls: list[float] = []


def handle_pressure(topic: str, data: dict[str, Any]) -> None:
    """Handle critical pressure events — trigger LLM diagnosis."""
    fingerprint = f"pressure_{data.get('level', 'unknown')}"
    if not _should_process(fingerprint):
        return

    bus = get_default_eventbus()
    agent = _get_agent(bus)
    if agent is None:
        return

    signal = {"type": "pressure_critical", **data}
    verdict = agent.diagnose(signal)

    if verdict.level == "escalated":
        bus.publish("tidy.escalation", verdict.to_dict(), source="tidy.subscriber")


def handle_leak(topic: str, data: dict[str, Any]) -> None:
    """Handle leak detection events — trigger LLM diagnosis."""
    owner = data.get("owner", "unknown")
    fingerprint = f"leak_{owner}"
    if not _should_process(fingerprint):
        return

    bus = get_default_eventbus()
    agent = _get_agent(bus)
    if agent is None:
        return

    signal = {"type": "leak_detected", **data}
    verdict = agent.diagnose(signal)

    bus.publish(
        "tidy.advisory",
        {
            "source": "leak_handler",
            **verdict.to_dict(),
        },
        source="tidy.subscriber",
    )


def handle_sweep_failure(topic: str, data: dict[str, Any]) -> None:
    """Handle sweep failure events — diagnose why cleanup failed."""
    fingerprint = f"sweep_{data.get('error', 'unknown')}"
    if not _should_process(fingerprint):
        return

    bus = get_default_eventbus()
    agent = _get_agent(bus)
    if agent is None:
        return

    signal = {"type": "sweep_failed", **data}
    agent.diagnose(signal)


# --- Circuit breakers ---


def _should_process(fingerprint: str) -> bool:
    """Check cooldown and rate limit. Returns True if we should proceed."""
    now = time.time()

    last_time = _recent_signals.get(fingerprint, 0)
    if now - last_time < COOLDOWN_SECONDS:
        return False

    _hourly_calls[:] = [t for t in _hourly_calls if now - t < 3600]
    if len(_hourly_calls) >= MAX_DIAGNOSES_PER_HOUR:
        return False

    _recent_signals[fingerprint] = now
    _hourly_calls.append(now)
    return True


def _get_agent(bus: Any):
    """Lazy-load the MoAgent with gateway. Returns None if unavailable."""
    try:
        from axiom.infra.gateway import Gateway

        gateway = Gateway()
        if not gateway.available:
            return None

        from . import manager
        from .agent import MoAgent

        agent = MoAgent(gateway=gateway, bus=bus)
        mgr = manager()

        try:
            from .network import NetworkLedger
            from .vitals import VitalsMonitor

            monitor = VitalsMonitor(mgr, NetworkLedger.shared(), bus)
            agent.set_manager(mgr, monitor)
        except Exception:
            agent.set_manager(mgr)

        return agent
    except Exception:
        return None
