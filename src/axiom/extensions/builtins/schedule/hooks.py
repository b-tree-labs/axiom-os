# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""PULSE lifecycle hooks.

The seam through which consumers and other agents plug behavior into the
schedule lifecycle without PULSE knowing anything about their domain. Two
flavors:

- ``pre_fire`` is a **gate** — registered checks can veto a fire (the
  precondition / allocation gate from the cross-industry design: "don't fire if
  the tool is down / the prerequisite is incomplete / no allocation"). It is
  **fail-closed**: if a check vetoes or raises, the fire is skipped. Safety
  domains (a beam, a dose) want "don't fire under uncertainty."
- All other points are **observational** — registered callbacks are notified
  and may act (calendar sync on reschedule, HERALD escalation on dead-letter,
  the anchor recompute on actual). Observational hooks never break the fire.

Every point also emits on the default EventBus (ADR-060), best-effort, so
cross-agent consumers subscribe without coupling. Emission never raises.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

logger = logging.getLogger("axiom.schedule.hooks")

# Hook points (also the EventBus subjects).
PRE_REGISTER = "schedule.pre_register"  # allocation gate (vetoing, at reserve time)
PRE_FIRE = "schedule.pre_fire"
ON_SUCCESS = "schedule.fired"
ON_FAILURE = "schedule.failed"
ON_DEAD_LETTER = "schedule.dead_letter"
ON_REGISTER = "schedule.registered"
ON_CANCEL = "schedule.cancelled"
ON_RESCHEDULE = "schedule.rescheduled"
ON_ACTUAL_RECORDED = "schedule.actual_recorded"
ON_CONFLICT = "schedule.conflict"

GateFn = Callable[[dict[str, Any]], Any]
ObserverFn = Callable[[dict[str, Any]], Any]

_registry: dict[str, list[Callable]] = {}


def register(point: str, fn: Callable) -> None:
    """Register a hook callback for a point. ``pre_fire`` callbacks may veto."""
    _registry.setdefault(point, []).append(fn)


def clear() -> None:
    """Drop all registered hooks (tests + re-init)."""
    _registry.clear()


def _vetoes(result: Any) -> bool:
    """A pre_fire result vetoes when it is explicitly falsy or a skip/deny token."""
    if result is None or result is True:
        return False
    if result is False:
        return True
    return str(result).lower() in ("skip", "deny", "veto", "defer", "block")


def gate(point: str, payload: dict[str, Any]) -> tuple[bool, str | None]:
    """Run a vetoing gate (``pre_fire``). Returns ``(proceed, reason)``.

    Fail-closed: a check that vetoes *or raises* blocks the fire.
    """
    for fn in _registry.get(point, []):
        try:
            result = fn(payload)
        except Exception as exc:  # noqa: BLE001 — fail-closed under uncertainty
            logger.warning("pre_fire hook %r raised; vetoing: %r", fn, exc)
            return False, f"precondition_error:{exc!r}"
        if _vetoes(result):
            return False, f"precondition_veto:{result!r}"
    return True, None


def emit(point: str, payload: dict[str, Any], *, bus: Any = None) -> None:
    """Notify observers + best-effort bus publish. Never raises."""
    for fn in _registry.get(point, []):
        try:
            fn(payload)
        except Exception as exc:  # noqa: BLE001 — observational, must not break the fire
            logger.warning("observer hook %r raised on %s: %r", fn, point, exc)
    _publish(point, payload, bus=bus)


def _publish(subject: str, payload: dict[str, Any], *, bus: Any = None) -> None:
    try:
        if bus is None:
            from axiom.infra.bus import get_default_eventbus

            bus = get_default_eventbus()
        bus.publish(subject, payload, source="pulse")
    except Exception:  # noqa: BLE001 — signalling is advisory
        pass


__all__ = [
    "ON_ACTUAL_RECORDED",
    "ON_CANCEL",
    "ON_CONFLICT",
    "ON_DEAD_LETTER",
    "ON_FAILURE",
    "ON_REGISTER",
    "ON_RESCHEDULE",
    "ON_SUCCESS",
    "PRE_FIRE",
    "PRE_REGISTER",
    "clear",
    "emit",
    "gate",
    "register",
]
