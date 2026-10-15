# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""In-process BusTransport with optional JSONL durability.

Today's `axiom.infra.orchestrator.bus.EventBus._dispatch` machinery refactored
behind the `BusTransport` Protocol. Behavior preserved (durability via
`locked_append_jsonl`, in-memory subscriber registry); only the seam moves.
"""

from __future__ import annotations

import threading
from collections.abc import Iterable
from pathlib import Path

from axiom.infra.bus.subjects import subject_matches, validate_pattern
from axiom.infra.bus.types import Event, Subscription
from axiom.infra.state import locked_append_jsonl


class InProcessTransport:
    """Single-process transport. Persists to JSONL; keeps subscribers in-memory.

    Args:
        log_path: Path to the durable .jsonl event log. None disables logging.
    """

    def __init__(self, log_path: Path | None = None) -> None:
        self._log_path = log_path
        # Registry mutex for thread-safe attach/detach against concurrent
        # `iter_subscribers` (publish-time read). See spec §10.5.
        self._lock = threading.RLock()
        self._subscriptions: list[Subscription] = []

    def accept(self, event: Event) -> None:
        """Persist the event to the JSONL log if durable; otherwise no-op.

        Note: this method does NOT dispatch to subscribers. The bus walks
        `iter_subscribers` separately on publish.
        """
        if self._log_path is not None:
            locked_append_jsonl(self._log_path, event.to_dict())

    def attach_subscriber(self, subscription: Subscription) -> None:
        """Register a subscriber. Pattern is validated; raises on malformed."""
        validate_pattern(subscription.pattern)
        with self._lock:
            self._subscriptions.append(subscription)

    def detach_subscriber(self, subscription: Subscription) -> None:
        """Remove a subscription. No-op if not registered."""
        with self._lock:
            self._subscriptions = [s for s in self._subscriptions if s is not subscription]

    def iter_subscribers(self, subject: str) -> Iterable[Subscription]:
        """Yield matching subscribers in ascending priority order.

        The returned list is a fire-time snapshot — callers may safely
        register/unregister handlers from inside a dispatched handler without
        affecting the in-flight delivery.
        """
        with self._lock:
            snapshot = list(self._subscriptions)
        matches = [sub for sub in snapshot if subject_matches(subject, sub.pattern)]
        matches.sort(key=lambda s: s.priority)
        return matches

    def iter_pending(self) -> Iterable[Event]:
        """In-process transport delivers synchronously; nothing pends."""
        return ()

    def durability_log_path(self) -> Path | None:
        return self._log_path
