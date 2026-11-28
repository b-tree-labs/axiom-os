# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""RotationEngine — drives a strategy, owns the overlap/revoke schedule.

The engine is the one place that decides *whether* to rotate (cadence vs.
force), invokes the selected ``RotationStrategy`` to mint+stage the new
credential, and then either revokes the old credential inline (zero
overlap) or defers the revoke until the overlap window closes. A scheduled
sweep (``run_due_revocations``) fires the deferred revokes; on the PULSE
substrate that sweep is a recurring job, but the engine keeps its own
in-memory pending list so it's testable and usable without a scheduler.

Time is injected (``clock``) rather than read from the wall clock, so
rotation is deterministic under test and replayable.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import Any, Callable

from ..providers.protocol import SecretRef
from .strategy import RotationOutcome, RotationPolicy, RotationStrategy

_log = logging.getLogger(__name__)


class NotDue(Exception):
    """Raised when a non-forced rotation is requested before its cadence."""


class RotationEngine:
    """Orchestrates rotations and the deferred revoke of aged-out creds.

    ``resolver(ref)`` selects the ``RotationStrategy`` for a ref (usually a
    registry lookup keyed by the ref's configured rotation kind).
    ``store_for(scheme)`` yields the ``SecretStore`` the new credential is
    written through. ``clock()`` returns the current epoch seconds.
    """

    def __init__(
        self,
        *,
        resolver: Callable[[SecretRef], RotationStrategy],
        store_for: Callable[[str], Any],
        clock: Callable[[], float],
    ) -> None:
        self._resolver = resolver
        self._store_for = store_for
        self._clock = clock
        # (revoke_at, ref, outcome, strategy, store) awaiting the window's close
        self._pending: list[tuple[float, SecretRef, RotationOutcome, RotationStrategy, Any]] = []

    def rotate(
        self,
        ref: SecretRef,
        *,
        policy: RotationPolicy,
        force: bool = False,
        last_rotated_at: float | None = None,
    ) -> RotationOutcome:
        """Rotate ``ref`` now (``force``) or if its cadence is due.

        Raises :class:`NotDue` when not forced and the policy cadence has
        not elapsed. Returns the :class:`RotationOutcome`; the old
        credential's revoke is either done inline (zero/elapsed overlap) or
        queued for :meth:`run_due_revocations`.
        """
        now = self._clock()
        if not force and not policy.is_due(last_rotated_at, now):
            raise NotDue(
                f"{ref} not due for rotation (cadence={policy.cadence_seconds}s, "
                f"last={last_rotated_at}); pass force=True to override"
            )

        strategy = self._resolver(ref)
        store = self._store_for(ref.scheme)
        outcome = strategy.perform(ref, store, now=now, policy=policy)
        outcome = replace(outcome, forced=force)

        if outcome.revoke_at is None or outcome.revoke_at <= now:
            strategy.revoke_previous(ref, store, outcome)
        else:
            self._pending.append((outcome.revoke_at, ref, outcome, strategy, store))
        return outcome

    def pending_revocations(self) -> int:
        """How many aged-out credentials are awaiting their revoke window."""
        return len(self._pending)

    def run_due_revocations(self) -> list[SecretRef]:
        """Revoke every old credential whose overlap window has closed.

        Idempotent: entries are dropped as they fire, so a second call with
        no further elapsed time revokes nothing.
        """
        now = self._clock()
        done: list[SecretRef] = []
        still_pending = []
        for entry in self._pending:
            revoke_at, ref, outcome, strategy, store = entry
            if revoke_at <= now:
                strategy.revoke_previous(ref, store, outcome)
                done.append(ref)
            else:
                still_pending.append(entry)
        self._pending = still_pending
        return done


__all__ = ["RotationEngine", "NotDue"]
