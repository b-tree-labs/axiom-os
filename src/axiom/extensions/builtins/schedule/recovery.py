# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Startup reconciliation of interrupted fires.

A crash between claiming an instant and recording its outcome leaves a
``pending`` fire-log row. On restart the engine must reconcile these — never
silently double-run a non-idempotent action:

- **Reentrant** action (declared safe to re-run): release the claim by deleting
  the pending row; the still-due instant fires again on the next tick.
- **Non-reentrant**: mark the row ``interrupted`` and surface it, AND advance
  the schedule past the wedged instant so the claim can't block all future
  fires (the claim stays, guaranteeing no double-run of *that* instant).

Idempotency is the safety net throughout: the claim makes a double-execute of
the same instant structurally impossible.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from typing import Any


def current_owner() -> tuple[int, float | None]:
    """``(pid, start time)`` of THIS process: the identity a fire claim records.

    The start time is what makes the pair name one process: a reused pid has a
    different start. ``None`` when the platform cannot say (no ``ps``); the
    claim then carries the pid alone and liveness falls back to the pid check.
    """
    from axiom.infra import process_registry

    pid = os.getpid()
    info = process_registry.probe(pid)
    return pid, (info.start if info is not None else None)


def owner_is_alive(pid: int | None, started_at: float | None) -> bool | None:
    """Is the process that claimed a fire still running?

    ``True``/``False``, or ``None`` when the claim recorded no owner (a row
    from before owners were recorded): the caller then has only the row's age.
    A zombie is not alive. A live pid whose start time disagrees with the
    record is a different process that inherited the number: not alive.
    """
    if pid is None:
        return None
    from axiom.infra import process_registry

    info = process_registry.probe(int(pid))
    if info is None:
        return False
    if started_at is not None and abs(info.start - started_at) > process_registry.START_TOLERANCE_S:
        return False
    return True


def reconcile_pending(
    now: datetime, *, stale_after_seconds: int = 300, owner_aware: bool = False
) -> dict[str, Any]:
    """Reconcile orphaned ``pending`` fire-log rows.

    Without ``owner_aware`` a row is an orphan once it is older than
    ``stale_after_seconds`` (the supervisor's SIGUSR1 passes 0: every pending
    row is then an orphan because no other copy runs).

    With ``owner_aware`` (the engine's own start-up) the row's recorded owner
    decides, and no signal is needed: a fire whose owner process is **dead** is
    an orphan at any age; one whose owner is **alive** belongs to a copy still
    working and is left alone; a row with no recorded owner (written before
    owners were recorded) falls back to the age rule.

    Returns ``{"reran": [...schedule_ids], "flagged": [...schedule_ids]}``.
    """
    from axiom.extensions.builtins.schedule import hooks, store
    from axiom.extensions.builtins.schedule.api import cadence_from_payload
    from axiom.extensions.builtins.schedule.cadence import compute_next_fire_at
    from axiom.extensions.builtins.schedule.db_models import (
        ScheduleDefinition,
        ScheduleFireLog,
    )

    cutoff = now - timedelta(seconds=stale_after_seconds)
    reran: list[str] = []
    flagged: list[str] = []

    def orphaned(r: Any) -> bool:
        if owner_aware:
            alive = owner_is_alive(r.owner_pid, r.owner_started_at)
            if alive is not None:
                return not alive
        started = r.started_at
        if started is not None and started.tzinfo is None:
            started = started.replace(tzinfo=cutoff.tzinfo)
        return started is not None and started < cutoff

    with store.session_scope() as s:
        rows = s.query(ScheduleFireLog).filter(ScheduleFireLog.outcome == "pending").all()
        for r in rows:
            if not orphaned(r):
                continue
            defn = s.get(ScheduleDefinition, r.schedule_id)
            if defn is not None and defn.reentrant:
                # Release the claim — the still-due instant re-fires next tick.
                s.delete(r)
                reran.append(r.schedule_id)
            else:
                r.outcome = "interrupted"
                r.error_summary = "reconciled after engine restart"
                if defn is not None:
                    cadence = cadence_from_payload(
                        defn.cadence_kind, defn.cadence_payload
                    )
                    defn.next_fire_at = compute_next_fire_at(
                        cadence, last_fire=r.intended_fire_at, now=now
                    )
                flagged.append(r.schedule_id)
        s.commit()

    for sid in flagged:
        hooks.emit(hooks.ON_FAILURE, {"schedule_id": sid, "reason": "interrupted_reconciled"})

    return {"reran": reran, "flagged": flagged}


__all__ = ["current_owner", "owner_is_alive", "reconcile_pending"]
