# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Blackout windows — suppress fires during a maintenance outage / holiday /
closure. Instants that fall inside an active blackout are skipped; the schedule
resumes after the window (no flood). A blackout is global, or scoped to a
``resource_key``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Optional


def add_blackout(
    starts_at: datetime,
    ends_at: datetime,
    *,
    resource_key: Optional[str] = None,
    reason: str = "",
    now: Optional[datetime] = None,
) -> str:
    """Declare a blackout window. Returns its id."""
    from axiom.extensions.builtins.schedule import store
    from axiom.extensions.builtins.schedule.db_models import ScheduleBlackout

    bid = uuid.uuid4().hex
    with store.session_scope() as s:
        s.add(ScheduleBlackout(
            id=bid, starts_at=starts_at, ends_at=ends_at,
            resource_key=resource_key, reason=reason,
            created_at=now or datetime.now(UTC),
        ))
        s.commit()
    return bid


def lift_blackout(blackout_id: str) -> bool:
    """Remove a blackout. Returns True if one was removed."""
    from axiom.extensions.builtins.schedule import store
    from axiom.extensions.builtins.schedule.db_models import ScheduleBlackout

    with store.session_scope() as s:
        row = s.get(ScheduleBlackout, blackout_id)
        if row is None:
            return False
        s.delete(row)
        s.commit()
        return True


def in_blackout(now: datetime, *, resource_key: Optional[str] = None) -> bool:
    """Is ``now`` inside an active blackout? A global blackout (NULL
    resource_key) matches everything; a scoped one matches only its resource."""
    from axiom.extensions.builtins.schedule import store
    from axiom.extensions.builtins.schedule.db_models import ScheduleBlackout

    with store.session_scope() as s:
        rows = (
            s.query(ScheduleBlackout)
            .filter(ScheduleBlackout.starts_at <= now)
            .filter(ScheduleBlackout.ends_at > now)
            .all()
        )
        for r in rows:
            if r.resource_key is None or r.resource_key == resource_key:
                return True
    return False


__all__ = ["add_blackout", "in_blackout", "lift_blackout"]
