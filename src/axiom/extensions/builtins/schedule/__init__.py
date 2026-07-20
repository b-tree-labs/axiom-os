# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""PULSE — app-level domain scheduler.

Per ADR-055 + prd-axiom-schedule + spec-axiom-schedule: every recurring
or one-shot domain event the platform fires goes through PULSE, which
constructs an :class:`ActionEnvelope`, consults ``authz.decide``,
executes the action, and writes a receipt.

PULSE-1 (this scaffold) ships single-node firing of cron + interval
cadences with idempotency + retry + dead-letter + manifest registration
+ CLI. Distributed firing, trigger-style schedules, and the federation
handoff land in PULSE-2 / PULSE-3.

Quick consumer integration::

    from axiom.extensions.builtins.schedule import register, Cadence
    from datetime import timedelta

    sched_id = register(
        envelope=envelope,
        cadence=Cadence(kind="interval", interval=timedelta(hours=1)),
        action="hygiene.scheduled.heartbeat",
        description="TIDY heartbeat",
    )
"""

from __future__ import annotations

from pathlib import Path

from axiom.extensions.builtins.schedule.api import (
    Cadence,
    ScheduleId,
    cancel,
    fire_now,
    list_schedules,
    pause,
    register,
    resume,
    status,
)

# PULSE persona path — consumed by the AEOS extension manifest's
# [[extension.provides]] agent block.
pulse_persona_path = str(
    Path(__file__).parent / "agents" / "pulse" / "persona.md"
)


__all__ = [
    "Cadence",
    "ScheduleId",
    "cancel",
    "fire_now",
    "list_schedules",
    "pause",
    "pulse_persona_path",
    "register",
    "resume",
    "status",
]
