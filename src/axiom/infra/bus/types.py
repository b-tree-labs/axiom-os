# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Core data types for the v2 event bus.

`Event` and `Subscription` are stable across every transport implementation.
`FailMode` enumerates the per-subscriber failure semantics described in
spec-event-bus.md §7.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

# Sync handler signature: (subject, payload) -> None.
# Async handler returns a coroutine; we don't enforce this at the type level
# because Python's runtime cares whether the result is awaitable, not the
# annotation.
EventHandler = Callable[[str, dict[str, Any]], Any]

# Per-subscriber failure mode. See spec-event-bus.md §7.
FailMode = Literal["abort", "warn", "ignore"]


@dataclass
class Event:
    """A single event flowing through the bus.

    Attributes:
        subject: NATS-shape subject (e.g., "tool.post_invoke").
        payload: Event payload; arbitrary JSON-serializable dict.
        timestamp: ISO-8601 string set in __post_init__ if blank.
        source: Publishing component identifier.
        envelope: Optional signed-envelope hook for federation transports;
            empty for in-process delivery.
    """

    subject: str
    payload: dict[str, Any]
    timestamp: str = ""
    source: str = ""
    envelope: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.timestamp:
            self.timestamp = datetime.now(UTC).isoformat()

    def to_dict(self) -> dict[str, Any]:
        return {
            "subject": self.subject,
            "payload": self.payload,
            "timestamp": self.timestamp,
            "source": self.source,
            "envelope": self.envelope,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Event:
        return cls(
            subject=d["subject"],
            payload=d.get("payload", {}),
            timestamp=d.get("timestamp", ""),
            source=d.get("source", ""),
            envelope=d.get("envelope", {}),
        )


@dataclass(frozen=True)
class Subscription:
    """An active subscriber to a subject pattern.

    Subscriptions are immutable. Mutating one means unregistering and
    registering again (the bus owns identity via the dataclass `__hash__`).
    """

    pattern: str
    handler: EventHandler
    is_async: bool
    priority: int = 100  # Lower runs first.
    fail_mode: FailMode = "warn"
    source: str = ""  # Extension name, "user", "platform", or "".
