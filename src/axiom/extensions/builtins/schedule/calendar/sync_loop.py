# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Two-way sync loop: keep PULSE time-slots and a calendar in agreement (track A).

PULSE owns the truth (Q7c); the calendar mirrors. ``push_slot_status`` upserts a
slot's calendar event (idempotent by stamp). ``ingest`` pulls events and hands
externally-authored ones (no stamp) to a caller-supplied ``register_slot`` —
someone booked directly on the calendar. ``make_reschedule_sync`` is a PULSE
``on_reschedule`` hook that pushes the moved slot to the calendar automatically.
"""

from __future__ import annotations

from typing import Any, Callable


def push_slot_status(provider: Any, *, calendar_id: str, status: dict) -> Any:
    """Upsert the calendar event for a PULSE time-slot (its `time_slot_status`)."""
    from axiom.extensions.builtins.schedule.calendar.sync import push_time_slot

    metadata = status.get("metadata") or {}
    return push_time_slot(
        provider,
        calendar_id=calendar_id,
        time_slot_id=status["time_slot_id"],
        summary=metadata.get("summary", status["time_slot_id"]),
        start=status["planned_start"],
        end=status.get("planned_end"),
    )


def ingest(
    provider: Any,
    *,
    calendar_id: str,
    window_start,
    window_end,
    register_slot: Callable[[Any], Any],
) -> list:
    """Pull events; hand externally-authored ones (no PULSE stamp) to
    ``register_slot``. Returns whatever it returns (e.g. new slot ids)."""
    from axiom.extensions.builtins.schedule.calendar.sync import pull_events

    created = []
    for item in pull_events(provider, calendar_id=calendar_id,
                            window_start=window_start, window_end=window_end):
        if item["pulse_id"] is None:        # operator/walk-in booked directly
            created.append(register_slot(item["event"]))
    return created


def make_reschedule_sync(
    provider: Any, calendar_id: str, resolve_status: Callable[[str], dict]
) -> Callable[[dict], None]:
    """A PULSE ``on_reschedule`` hook: when a slot moves, push the update to the
    calendar (the linked event follows). Best-effort — never breaks the fire."""
    def hook(payload: dict) -> None:
        slot_id = payload.get("time_slot_id")
        if not slot_id:
            return
        try:
            push_slot_status(provider, calendar_id=calendar_id,
                             status=resolve_status(slot_id))
        except Exception:  # noqa: BLE001 — sync is advisory
            pass
    return hook


__all__ = ["ingest", "make_reschedule_sync", "push_slot_status"]
