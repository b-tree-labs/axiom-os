# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Single-node leader lease for PULSE-1.

Per spec-axiom-schedule §1: a singleton row in ``schedule.schedule_lease``
holds the active engine's identity. Acquisition uses a Postgres advisory
lock to bound the contention window; renewal happens every
``lease_ttl_seconds / 3``.

PULSE-1 is single-node by construction; the lease is uncontested in
practice. The same code path is what PULSE-2 will run in distributed
mode — the only diff there is on-failure handoff timing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional


@dataclass
class Lease:
    """An in-memory view of the active lease. Engines treat ``held()`` as
    authoritative; the row in Postgres is the source of truth at
    acquisition / renewal boundaries.
    """

    node_id: str
    acquired_at: datetime
    expires_at: datetime
    renewed_at: datetime

    def held(self, now: datetime) -> bool:
        return now < self.expires_at


class LeaseManager:
    """The acquire / renew / release surface the engine consults.

    PULSE-1 implementation is the test-driven stub — the test suite
    drives the in-DB path once the integration harness is wired.
    """

    def __init__(self, node_id: str, ttl_seconds: int = 30) -> None:
        self.node_id = node_id
        self.ttl = timedelta(seconds=ttl_seconds)
        self._current: Optional[Lease] = None

    def try_acquire(self, now: datetime) -> Optional[Lease]:
        """Attempt to claim the lease. Returns the lease on success, None on contention."""
        # In-memory fallback for PULSE-1 single-node — always acquires.
        # The Postgres-backed variant lands as test_lease_single_node drives it.
        if self._current is not None and self._current.held(now):
            if self._current.node_id == self.node_id:
                return self._current
            return None
        lease = Lease(
            node_id=self.node_id,
            acquired_at=now,
            expires_at=now + self.ttl,
            renewed_at=now,
        )
        self._current = lease
        return lease

    def maybe_renew(self, now: datetime) -> None:
        """Renew if the lease's remaining TTL crossed the renewal threshold."""
        if self._current is None:
            return
        elapsed = now - self._current.renewed_at
        if elapsed >= self.ttl / 3:
            self._current = Lease(
                node_id=self._current.node_id,
                acquired_at=self._current.acquired_at,
                expires_at=now + self.ttl,
                renewed_at=now,
            )

    def held(self, now: datetime) -> bool:
        return self._current is not None and self._current.held(now)

    def release(self, now: datetime) -> None:
        self._current = None


__all__ = ["Lease", "LeaseManager"]
