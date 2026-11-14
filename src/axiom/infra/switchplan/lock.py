# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""One single-flight lock for a switch, with two backends (ADR-197 D7).

*Advisory* is a session advisory lock on a database connection: it dies with the
connection, so a killed process releases it. *Lease* is the pending-claim file
primitive (atomic link, pid and start time, stale takeover) for kinds with no
database. Both answer "is anyone else running this switch right now"; the pending
claim of ADR-182 D3a answers a different question (which copy finishes an item).
"""

from __future__ import annotations

import contextlib
import hashlib
import re
from pathlib import Path

from axiom.infra import pending_claim
from axiom.infra.switchplan.errors import SwitchBusy

__all__ = ["SingleFlight", "advisory_key"]


def advisory_key(name: str) -> str:
    """The text hashed into the advisory-lock key; one scheme for every user."""
    return f"axiom.switch:{name}"


class SingleFlight:
    """``SingleFlight(name, conn=...)`` or ``SingleFlight(name, lease_dir=...)``.

    Re-entrant acquire is refused, not counted: a second `acquire()` on a held
    lock returns False.
    """

    def __init__(self, name: str, *, conn=None, lease_dir: str | Path | None = None, stale_after_s: float | None = None) -> None:
        if (conn is None) == (lease_dir is None):
            raise ValueError("give exactly one of conn (advisory) or lease_dir (file lease)")
        self.name = name
        self._conn = conn
        self._lease_dir = Path(lease_dir) if lease_dir is not None else None
        self._stale_after_s = stale_after_s
        self._claim: pending_claim.Claim | None = None
        self._held_advisory = False

    @property
    def held(self) -> bool:
        return self._held_advisory or self._claim is not None

    def acquire(self) -> bool:
        if self.held:
            return False
        if self._conn is not None:
            row = self._conn.execute(
                "SELECT pg_try_advisory_lock(hashtextextended(%s, 0))", (advisory_key(self.name),)
            ).fetchone()
            self._held_advisory = bool(row[0])
            return self._held_advisory
        assert self._lease_dir is not None
        self._lease_dir.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", self.name)[:80] + "-" + hashlib.sha1(self.name.encode()).hexdigest()[:8]
        self._claim = pending_claim.claim(self._lease_dir / safe, stale_after_s=self._stale_after_s)
        return self._claim is not None

    def blocking(self, timeout_s: float) -> bool:
        """Wait up to ``timeout_s`` for the lock (the schema-DDL serializer's mode)."""
        import time

        deadline = time.monotonic() + timeout_s
        while True:
            if self.acquire():
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.05)

    def touch(self) -> None:
        if self._claim is not None:
            self._claim.touch()

    def release(self) -> None:
        if self._held_advisory and self._conn is not None:
            with contextlib.suppress(Exception):  # a dropped connection already released it
                self._conn.execute("SELECT pg_advisory_unlock(hashtextextended(%s, 0))", (advisory_key(self.name),))
            self._held_advisory = False
        if self._claim is not None:
            self._claim.release()
            self._claim = None

    def __enter__(self) -> SingleFlight:
        if not self.acquire():
            raise SwitchBusy(f"{self.name} is being switched by another process; nothing was started")
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()
