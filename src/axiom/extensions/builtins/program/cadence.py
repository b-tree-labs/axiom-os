# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The program's own schedule on PULSE: sync and the consolidation pass (prd-program R28).

The rows are put on the schedule **by declaration**: the program manifest
declares ``program.sync`` (every 15 minutes), ``program.consolidate`` (weekly)
and the follow-up sweep as ``[[extension.schedule]]`` blocks with ``hosted =
true``, and the host that runs PULSE registers them when it starts
(``schedule.install.sync_hosted_schedules``) without naming this extension.

This module lets the program **retune its own rows** to the data file's
``program.cadence``, so the interval stays configurable per program without
the host knowing about it. The scheduled sync calls :func:`retune` after each
run: a changed interval or schedule reschedules the row, ``enabled: false`` /
``consolidate_enabled: false`` pause it, and re-enabling resumes it. A missing
row (the host has not started since the upgrade) is registered.

The PULSE lease keeps one leader per node, the fire log claims each instant
once, ``fire_once`` runs an overdue instant once after downtime, and the sync
skill's own lock (``skills.sync.single_flight``) keeps two runs from
overlapping even when a node-local timer is still installed beside it.

Configuration, read from the node's data file without validation
(``program.cadence``)::

    {"sync_minutes": 15, "enabled": true,
     "consolidate": "0 8 * * 1", "consolidate_enabled": true}
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

SYNC_ACTION = "program.sync"
CONSOLIDATE_ACTION = "program.consolidate"
EXTENSION = "program"

DEFAULT_SYNC_MINUTES = 15
#: Monday 08:00 UTC: one digest per week, before the working week starts.
DEFAULT_CONSOLIDATE_SCHEDULE = "0 8 * * 1"

#: What the scheduled sync asks for: single-flight, a quiet no-op without a
#: program, and the publish chain (itself off unless the program enables it).
SYNC_PARAMS: dict[str, Any] = {"scheduled": True, "single_flight": True, "publish": True}
CONSOLIDATE_PARAMS: dict[str, Any] = {"scheduled": True, "deliver": True}

_log = logging.getLogger("axiom.program.cadence")


def cadence_config(state_dir: Path | str) -> dict[str, Any]:
    """``program.cadence`` from the node's data file, defaults filled in."""
    config: dict[str, Any] = {
        "sync_minutes": DEFAULT_SYNC_MINUTES,
        "enabled": True,
        "consolidate": DEFAULT_CONSOLIDATE_SCHEDULE,
        "consolidate_enabled": True,
    }
    try:
        doc = json.loads((Path(state_dir) / "program" / "data.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return config
    declared = (doc.get("program") or {}).get("cadence") if isinstance(doc, dict) else None
    if not isinstance(declared, dict):
        return config
    minutes = declared.get("sync_minutes")
    if isinstance(minutes, int) and not isinstance(minutes, bool) and minutes >= 1:
        config["sync_minutes"] = minutes
    for key in ("enabled", "consolidate_enabled"):
        if isinstance(declared.get(key), bool):
            config[key] = declared[key]
    schedule = declared.get("consolidate")
    if isinstance(schedule, str) and schedule.strip():
        config["consolidate"] = schedule.strip()
    return config


def _matches(row: Any, cadence: Any) -> bool:
    payload = row.cadence_payload or {}
    if row.cadence_kind != cadence.kind:
        return False
    if cadence.kind == "interval":
        return payload.get("interval_seconds") == int(cadence.interval.total_seconds())
    if cadence.kind == "cron":
        return payload.get("cron_expr") == cadence.cron
    if cadence.kind == "rrule":
        return payload.get("rrule") == cadence.rrule
    return True


def _ensure(action: str, cadence: Any, *, params: dict[str, Any], enabled: bool,
            description: str, now: datetime | None) -> str:
    """One idempotent row for ``action``: register, resume, reschedule or pause it."""
    from axiom.extensions.builtins.schedule import api as pulse
    from axiom.extensions.builtins.schedule import store
    from axiom.extensions.builtins.schedule.db_models import ScheduleDefinition

    with store.session_scope() as s:
        row = (
            s.query(ScheduleDefinition)
            .filter(ScheduleDefinition.action == action, ScheduleDefinition.state != "cancelled")
            .order_by(ScheduleDefinition.created_at)
            .first()
        )
        found = (row.id, row.state, _matches(row, cadence)) if row is not None else None
    if found is None:
        if not enabled:
            return "disabled"
        sid = pulse.register(
            envelope={"action": action, "params": dict(params)},
            cadence=cadence,
            action=action,
            description=description,
            extension=EXTENSION,
            retry_policy={"max_attempts": 1},
            misfire_policy="fire_once",
            now=now,
        )
        return f"registered:{sid}"
    sid, state, same = found
    if not enabled:
        if state == "active":
            pulse.pause(pulse.ScheduleId(sid), "program.cadence disables it", now=now)
            return f"paused:{sid}"
        return f"disabled:{sid}"
    outcome = f"unchanged:{sid}"
    if state == "paused":
        pulse.resume(pulse.ScheduleId(sid), now=now)
        outcome = f"resumed:{sid}"
    if not same:
        pulse.reschedule(pulse.ScheduleId(sid), cadence=cadence, now=now)
        outcome = f"rescheduled:{sid}"
    return outcome


def ensure_sync(state_dir: Path | str, *, now: datetime | None = None) -> str:
    """Put ``program.sync`` on PULSE at the configured interval (default 15 minutes)."""
    from axiom.extensions.builtins.schedule.api import Cadence

    config = cadence_config(state_dir)
    minutes = config["sync_minutes"]
    return _ensure(
        SYNC_ACTION,
        Cadence(kind="interval", interval=timedelta(minutes=minutes)),
        params=SYNC_PARAMS,
        enabled=config["enabled"],
        description=f"program sync every {minutes} min (single-flight)",
        now=now,
    )


def ensure_consolidate(state_dir: Path | str, *, now: datetime | None = None) -> str:
    """Put the weekly consolidation pass on PULSE (default Monday 08:00)."""
    from axiom.extensions.builtins.schedule.formats import parse

    config = cadence_config(state_dir)
    schedule = config["consolidate"]
    try:
        cadence = parse(schedule)
    except Exception as exc:  # noqa: BLE001 - a bad schedule string is reported, never crashes the host
        _log.error("program.cadence.consolidate %r is not a schedule: %s", schedule, exc)
        return f"invalid:{schedule}"
    return _ensure(
        CONSOLIDATE_ACTION,
        cadence,
        params=CONSOLIDATE_PARAMS,
        enabled=config["consolidate_enabled"],
        description=f"program consolidation digest @ {schedule}",
        now=now,
    )


def retune(state_dir: Path | str, *, now: datetime | None = None) -> dict[str, str]:
    """:func:`ensure_all`, quiet: a node without a scheduler database is not an error."""
    try:
        from axiom.extensions.builtins.schedule import store

        with store.session_scope() as s:
            s.connection()
    except Exception as exc:  # noqa: BLE001
        _log.info("program cadence not retuned: no schedule store (%s)", type(exc).__name__)
        return {"skipped": "no schedule store"}
    return ensure_all(state_dir, now=now)


def ensure_all(state_dir: Path | str, *, now: datetime | None = None) -> dict[str, str]:
    """Both rows; each in its own try so one failing does not stop the other."""
    report: dict[str, str] = {}
    for action, fn in ((SYNC_ACTION, ensure_sync), (CONSOLIDATE_ACTION, ensure_consolidate)):
        try:
            report[action] = fn(state_dir, now=now)
        except Exception as exc:  # noqa: BLE001
            _log.exception("could not put %s on the schedule", action)
            report[action] = f"error:{type(exc).__name__}"
    return report


__all__ = [
    "CONSOLIDATE_ACTION",
    "CONSOLIDATE_PARAMS",
    "DEFAULT_CONSOLIDATE_SCHEDULE",
    "DEFAULT_SYNC_MINUTES",
    "SYNC_ACTION",
    "SYNC_PARAMS",
    "cadence_config",
    "ensure_all",
    "ensure_consolidate",
    "ensure_sync",
    "retune",
]
