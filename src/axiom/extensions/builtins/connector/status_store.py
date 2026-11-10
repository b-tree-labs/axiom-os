# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Connector-status store — the read side of the observability surface.

Subscribes to ``connector.*`` events from ``observability.py`` and keeps
the last-known outcome per connector + a small rolling history. The
status CLI (``axi notifications connector status``) reads from it; the
reconnect skill (``axi notifications connector reconnect``) consults it
to identify the failing connector + its last error.

The store is intentionally in-process + in-memory for v0. The Postgres-
backed durable variant lands in a follow-up alongside the audit-log
retention work (the Axiom Cloud bucket). Same Protocol surface so the
swap is transparent.
"""

from __future__ import annotations

import threading
from collections import deque
from typing import Any, Protocol

from axiom.extensions.builtins.connector.observability import (
    SUBJECT_DELIVERED,
    SUBJECT_FAILED,
    SUBJECT_RECONNECT_REQUIRED,
    ConnectorOutcome,
)


# ---------------------------------------------------------------------------
# Store Protocol
# ---------------------------------------------------------------------------


class ConnectorStatusStore(Protocol):
    """Read/write Protocol every status backend implements."""

    def record(self, outcome: ConnectorOutcome) -> None: ...

    def latest(self, connector: str) -> ConnectorOutcome | None: ...

    def all_latest(self) -> dict[str, ConnectorOutcome]: ...

    def reconnect_pending(self) -> list[ConnectorOutcome]:
        """Subset of ``all_latest`` whose ``reconnect_required`` is True."""

    def history(
        self, connector: str, limit: int = 50
    ) -> list[ConnectorOutcome]: ...


# ---------------------------------------------------------------------------
# In-memory backend
# ---------------------------------------------------------------------------


class InMemoryStatusStore:
    """Process-local backend. Thread-safe for the bus's sync dispatch."""

    def __init__(self, history_per_connector: int = 200) -> None:
        self._limit = history_per_connector
        self._latest: dict[str, ConnectorOutcome] = {}
        self._history: dict[str, deque[ConnectorOutcome]] = {}
        self._lock = threading.Lock()

    def record(self, outcome: ConnectorOutcome) -> None:
        with self._lock:
            self._latest[outcome.connector] = outcome
            buf = self._history.setdefault(
                outcome.connector, deque(maxlen=self._limit)
            )
            buf.append(outcome)

    def latest(self, connector: str) -> ConnectorOutcome | None:
        with self._lock:
            return self._latest.get(connector)

    def all_latest(self) -> dict[str, ConnectorOutcome]:
        with self._lock:
            return dict(self._latest)

    def reconnect_pending(self) -> list[ConnectorOutcome]:
        with self._lock:
            return [
                o for o in self._latest.values() if o.reconnect_required
            ]

    def history(
        self, connector: str, limit: int = 50
    ) -> list[ConnectorOutcome]:
        with self._lock:
            buf = self._history.get(connector)
            if not buf:
                return []
            items = list(buf)[-limit:]
        return items


# ---------------------------------------------------------------------------
# Bus subscriber
# ---------------------------------------------------------------------------


class StatusStoreSubscriber:
    """Wire a bus to a store. ``attach(bus)`` subscribes; ``detach`` cleans up.

    Mirrors ``AgentBridge`` lifecycle so the two compose: a deployment
    typically attaches both — AgentBridge fans out to humans, this one
    holds the canonical operator-visible state.
    """

    def __init__(self, store: ConnectorStatusStore) -> None:
        self._store = store
        self.subscriptions: list[Any] = []

    def attach(self, bus: Any) -> None:
        for subject in (
            SUBJECT_DELIVERED,
            SUBJECT_FAILED,
            SUBJECT_RECONNECT_REQUIRED,
        ):
            sub = bus.subscribe(
                subject,
                self._on_event,
                fail_mode="warn",
                source="notifications.status_store",
            )
            self.subscriptions.append(sub)

    def detach(self) -> None:
        for sub in self.subscriptions:
            try:
                sub.unsubscribe()
            except Exception:  # noqa: BLE001
                pass
        self.subscriptions = []

    def _on_event(self, subject: str, payload: dict[str, Any]) -> None:
        outcome = ConnectorOutcome.from_payload(payload)
        try:
            self._store.record(outcome)
        except Exception:  # noqa: BLE001
            # Status-store failures must not poison the bus's other
            # subscribers (HERALD bridge etc.).
            pass


# ---------------------------------------------------------------------------
# Module-level singleton — default store used by the CLI skills
# ---------------------------------------------------------------------------


_default_store: InMemoryStatusStore | None = None
_default_lock = threading.Lock()


def get_default_store() -> InMemoryStatusStore:
    """Lazy process-default store; the CLI status + reconnect skills read
    from it.  Tests inject their own store via the skill ``params``."""
    global _default_store
    with _default_lock:
        if _default_store is None:
            _default_store = InMemoryStatusStore()
        return _default_store


def reset_default_store_for_testing() -> None:
    """Clear the process-default — for test isolation only."""
    global _default_store
    with _default_lock:
        _default_store = None


__all__ = [
    "ConnectorStatusStore",
    "InMemoryStatusStore",
    "StatusStoreSubscriber",
    "get_default_store",
    "reset_default_store_for_testing",
]
