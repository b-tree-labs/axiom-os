# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Helpers for the ``reference-match`` tests: an attachment for a door set, and a run.

The tests time everything by the scenes' own clock, so :data:`TIMING` is short: a
label is reported after five seconds of frames, left after two, and a heartbeat is
far enough away that only the tests about heartbeats see one.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from axiom.extensions.builtins.video.reference_match import ReferenceMatchTrigger
from axiom.extensions.builtins.video.triggers import TriggerEvent

from .scenes import DoorSet, seconds_after_t0

TIMING: dict[str, float] = {"sample_s": 1.0, "dwell_s": 5.0, "leave_s": 2.0, "heartbeat_s": 600.0}


def attach(door_set: DoorSet, **settings: Any) -> dict[str, Any]:
    """An attachment's config for ``door_set``: what the factory is given."""
    return {"reference_set": str(door_set.path), **TIMING, **settings}


def trigger_for(door_set: DoorSet, **settings: Any) -> ReferenceMatchTrigger:
    trigger = ReferenceMatchTrigger(attach(door_set, **settings))
    trigger.start()
    return trigger


def run(trigger: ReferenceMatchTrigger, scene: Iterable[Any]) -> list[TriggerEvent]:
    out: list[TriggerEvent] = []
    for frame in scene:
        out.extend(trigger.observe(frame))
    return out


def watch(
    trigger: ReferenceMatchTrigger, scene: Iterable[Any], region: str = "door"
) -> tuple[list[tuple[float, str, tuple[str, ...]]], list[TriggerEvent]]:
    """``(seconds, state, reasons)`` of ``region`` after every frame, from its ``health()``,
    and every event the frames gave."""
    seen, events = [], []
    for frame in scene:
        events.extend(trigger.observe(frame))
        row = trigger.health()["regions"][region]
        seen.append((seconds_after_t0(frame.ts), row["state"], tuple(row["reasons"])))
    return seen, events


def kinds(events: Iterable[TriggerEvent]) -> list[str]:
    return [e.kind for e in events]


def changes(events: Iterable[TriggerEvent]) -> list[TriggerEvent]:
    """The ``started`` and ``ended`` events: the changes, without the moments."""
    return [e for e in events if e.kind in ("started", "ended")]


def at(event: TriggerEvent) -> float:
    return seconds_after_t0(event.ts)


__all__ = ["TIMING", "at", "attach", "changes", "kinds", "run", "trigger_for", "watch"]
