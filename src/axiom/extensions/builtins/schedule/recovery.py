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

from datetime import datetime, timedelta
from typing import Any


def reconcile_pending(now: datetime, *, stale_after_seconds: int = 300) -> dict[str, Any]:
    """Reconcile orphaned ``pending`` fire-log rows older than the threshold.

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

    with store.session_scope() as s:
        rows = (
            s.query(ScheduleFireLog)
            .filter(ScheduleFireLog.outcome == "pending")
            .filter(ScheduleFireLog.started_at < cutoff)
            .all()
        )
        for r in rows:
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


__all__ = ["reconcile_pending"]
