# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""BusTransport Protocol — the v2 swappable-backend seam.

A transport owns *where the event physically lives between publish and
dispatch*; the bus owns *what an event looks like and how subscribers see it*.

v2.0 ships one concrete transport (`InProcessTransport`). Future transports
(NATS JetStream, PostgreSQL LISTEN/NOTIFY) implement the same Protocol with
no subscriber-facing change.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Protocol, runtime_checkable

from axiom.infra.bus.types import Event, Subscription


@runtime_checkable
class BusTransport(Protocol):
    """Backend that physically delivers events between publish and dispatch."""

    def accept(self, event: Event) -> None:
        """Called by the bus on publish. Transport persists / forwards the event."""

    def attach_subscriber(self, subscription: Subscription) -> None:
        """Called by the bus when a subscriber is registered."""

    def detach_subscriber(self, subscription: Subscription) -> None:
        """Called by the bus when a subscriber is removed."""

    def iter_subscribers(self, subject: str) -> Iterable[Subscription]:
        """Yield subscribers whose pattern matches `subject`, in priority order."""

    def iter_pending(self) -> Iterable[Event]:
        """Drain events queued by the transport. Used by replay tooling."""

    def durability_log_path(self) -> Path | None:
        """Path to the JSONL log if durable; None if ephemeral."""
