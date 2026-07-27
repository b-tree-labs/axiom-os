# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Axiom event bus v2 — NATS-shape subjects, swappable transports.

Public API:

    from axiom.infra.bus import EventBus, Event, Subscription, FailMode

See `docs/specs/spec-event-bus.md` for the full design.
"""

from __future__ import annotations

import threading

from axiom.infra.bus.event_bus import EventBus
from axiom.infra.bus.in_process import InProcessTransport
from axiom.infra.bus.subjects import (
    InvalidSubjectError,
    subject_matches,
    validate_pattern,
    validate_subject,
)
from axiom.infra.bus.transport import BusTransport
from axiom.infra.bus.types import Event, EventHandler, FailMode, Subscription

# ---------------------------------------------------------------------------
# Process-default `EventBus` — needed by manifest-declared hook handlers
# that publish chained events (e.g., hygiene/diagnostics handlers that
# emit `tidy.escalation` or `doctor.patch_complete`). Production wiring
# sets this at startup; tests can replace via `set_default_eventbus()`.
# ---------------------------------------------------------------------------

_default_eventbus: EventBus | None = None
_default_eventbus_lock = threading.Lock()


def get_default_eventbus() -> EventBus:
    """Return the lazily-instantiated process default `EventBus`.

    The default is a vanilla `EventBus()` with no durable log. Production
    setup should call `set_default_eventbus()` early to swap in a bus
    wired to the canonical JSONL log path.
    """
    global _default_eventbus
    with _default_eventbus_lock:
        if _default_eventbus is None:
            _default_eventbus = EventBus()
        return _default_eventbus


def set_default_eventbus(bus: EventBus | None) -> None:
    """Replace the process default. Tests + early-startup production wiring."""
    global _default_eventbus
    with _default_eventbus_lock:
        _default_eventbus = bus


__all__ = [
    "BusTransport",
    "Event",
    "EventBus",
    "EventHandler",
    "FailMode",
    "InProcessTransport",
    "InvalidSubjectError",
    "Subscription",
    "get_default_eventbus",
    "set_default_eventbus",
    "subject_matches",
    "validate_pattern",
    "validate_subject",
]
