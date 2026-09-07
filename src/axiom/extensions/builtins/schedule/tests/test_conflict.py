# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Conflict detection on shared resources + the operator-veto reschedule flow
(propose → confirm/reject), including the immovable (fixed) slot rule."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from axiom.extensions.builtins.schedule import hooks, seam
from axiom.extensions.builtins.schedule.conflicts import ConflictError, find_conflicts

T0 = datetime(2026, 6, 8, 9, 0, 0, tzinfo=UTC)
HOUR = timedelta(hours=1)


def _n(dt):
    return dt.replace(tzinfo=None) if dt is not None and dt.tzinfo else dt


def test_overlapping_slots_on_same_resource_conflict(sqlite_store):
    a = seam.register_time_slot(
        planned_start=T0, planned_end=T0 + 2 * HOUR, resource_key="bay-3", now=T0
    )
    events = []
    hooks.register(hooks.ON_CONFLICT, events.append)
    seam.register_time_slot(  # overlaps a on bay-3
        planned_start=T0 + HOUR, planned_end=T0 + 3 * HOUR, resource_key="bay-3", now=T0
    )
    assert events and events[0]["conflicts"][0]["time_slot_id"] == a


def test_no_conflict_on_different_resource_or_disjoint_time(sqlite_store):
    seam.register_time_slot(
        planned_start=T0, planned_end=T0 + 2 * HOUR, resource_key="bay-3", now=T0
    )
    assert find_conflicts("bay-4", T0, T0 + 2 * HOUR) == []          # other resource
    assert find_conflicts("bay-3", T0 + 3 * HOUR, T0 + 4 * HOUR) == []  # disjoint time


def test_reject_on_conflict_raises(sqlite_store):
    seam.register_time_slot(
        planned_start=T0, planned_end=T0 + 2 * HOUR, resource_key="bay-3", now=T0
    )
    with pytest.raises(ConflictError):
        seam.register_time_slot(
            planned_start=T0 + HOUR, planned_end=T0 + 3 * HOUR,
            resource_key="bay-3", reject_on_conflict=True, now=T0,
        )


def test_propose_then_confirm_applies_the_move(sqlite_store):
    slot = seam.register_time_slot(planned_start=T0 + 2 * HOUR, now=T0)
    seam.propose_reschedule(slot, new_planned_start=T0 + 5 * HOUR)
    # Proposed, not applied.
    assert _n(seam.time_slot_status(slot)["planned_start"]) == _n(T0 + 2 * HOUR)
    seam.confirm_reschedule(slot, now=T0)
    assert _n(seam.time_slot_status(slot)["planned_start"]) == _n(T0 + 5 * HOUR)


def test_propose_then_reject_discards(sqlite_store):
    slot = seam.register_time_slot(planned_start=T0 + 2 * HOUR, now=T0)
    seam.propose_reschedule(slot, new_planned_start=T0 + 5 * HOUR)
    seam.reject_reschedule(slot)
    assert _n(seam.time_slot_status(slot)["planned_start"]) == _n(T0 + 2 * HOUR)
    assert seam.time_slot_status(slot)["proposed_planned_start"] is None


def test_confirm_refuses_onto_fixed_slot_but_allows_around_it(sqlite_store):
    seam.register_time_slot(  # immovable
        planned_start=T0 + 4 * HOUR, planned_end=T0 + 6 * HOUR,
        resource_key="bay-3", fixed=True, now=T0,
    )
    mover = seam.register_time_slot(
        planned_start=T0, planned_end=T0 + 2 * HOUR, resource_key="bay-3", now=T0
    )
    # Proposing onto the fixed slot's window: confirm must refuse.
    seam.propose_reschedule(
        mover, new_planned_start=T0 + 4 * HOUR + timedelta(minutes=30),
        new_planned_end=T0 + 5 * HOUR,
    )
    with pytest.raises(ConflictError):
        seam.confirm_reschedule(mover, now=T0)

    # Rescheduling *around* the fixed slot is fine.
    seam.propose_reschedule(mover, new_planned_start=T0 + 7 * HOUR, new_planned_end=T0 + 8 * HOUR)
    seam.confirm_reschedule(mover, now=T0)
    assert _n(seam.time_slot_status(mover)["planned_start"]) == _n(T0 + 7 * HOUR)
