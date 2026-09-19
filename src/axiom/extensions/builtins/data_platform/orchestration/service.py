# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The data-platform orchestrator service — PULSE's production host.

This is the ``data_platform_orchestrator`` service entry from the
extension manifest, previously a skeleton. It closes the audit gap
"PULSE stores cadences but nothing fires them in production" by hosting
the real dispatch chain:

- **Executor** — :class:`SkillExecutor` (schedule extension): a stored
  cadence's ``action`` string is a qualified skill name invoked through
  the SkillRegistry.
- **Tick loop** — :func:`engine.tick` driven at a configurable interval
  with an injectable clock/sleep (tests call :meth:`tick` manually — no
  wall-clock, no sleeps).
- **Single-flight** — two layers: the PULSE lease (single leader per
  node id) and the fire-log's ``(schedule_id, bucket, params_hash)``
  unique-constraint claim, so one instant fires exactly once even with
  two hosts pointed at the same store.
- **Misfire** — default cadences register with ``fire_once``: an
  overdue instant runs once, then ``next_fire_at`` jumps past the
  missed backlog (no flood after downtime).
- **Authz + audit** — every dispatch writes a ``dispatch`` receipt
  through the extension's ``_authz`` wiring at fire time (the skills
  additionally write their own verb receipts).
- **Alerting** — dead-lettered dispatches publish the HERALD event
  ``data.dispatch.dead_letter`` (skills publish their own
  ``data.backup.*`` events for in-skill failures).

On startup (and on demand) :meth:`ensure_backup_cadences` projects the
persisted :class:`BackupPolicy` onto PULSE: enabled → a ``data.backup``
cadence on ``policy.schedule`` + a ``data.backup_validate`` cadence on
``policy.validate_schedule`` (registered idempotently, rescheduled on
policy edits); disabled → both paused.
"""

from __future__ import annotations

import logging
import os
import socket
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from axiom.extensions.builtins.schedule import api as pulse_api
from axiom.extensions.builtins.schedule import hooks as pulse_hooks
from axiom.extensions.builtins.schedule import store as pulse_store
from axiom.extensions.builtins.schedule.api import Cadence, ScheduleId
from axiom.extensions.builtins.schedule.db_models import ScheduleDefinition
from axiom.extensions.builtins.schedule.engine import (
    EngineContext,
    TickReport,
)
from axiom.extensions.builtins.schedule.engine import (
    tick as engine_tick,
)
from axiom.extensions.builtins.schedule.executor import SkillExecutor
from axiom.extensions.builtins.schedule.lease import LeaseManager
from axiom.infra.paths import get_user_state_dir
from axiom.infra.skills import SkillRegistry

from .. import _authz, _herald
from ..database.backup_policy import (
    cadence_for,
    load_backup_policy,
    validate_policy,
)

_log = logging.getLogger("axiom.data_platform.orchestrator")

BACKUP_ACTION = "data.backup"
VALIDATE_ACTION = "data.backup_validate"

DEFAULT_TICK_INTERVAL_SECONDS = 30.0
_TICK_INTERVAL_ENV = "AXI_ORCHESTRATOR_TICK_SECONDS"


class DispatchAuthz:
    """The engine's fire-time ``AuthzClient``: one ``dispatch`` receipt
    per fired instant, written through the extension's GUARD wiring.

    A deny (``AuthorizationDenied``) returns ``False`` so the engine
    records ``authz_denied`` and never executes. GUARD-unavailable falls
    back to ``_authz``'s synthetic permit-with-log (dev posture)."""

    def decide(self, envelope: Any) -> bool:
        action = "unknown"
        if isinstance(envelope, dict):
            action = str(envelope.get("action") or "unknown")
        try:
            with _authz.action(
                verb="dispatch",
                resource=f"data-platform://schedule/{action}",
            ) as act:
                _log.info("dispatch authorized: %s (receipt %s)", action, act.receipt_id)
            return True
        except Exception as exc:  # noqa: BLE001 — deny is a result, not a crash
            _log.warning("dispatch DENIED for %s: %s", action, exc)
            return False


class OrchestratorService:
    """Long-running scheduler host (manifest service
    ``data_platform_orchestrator``).

    Deterministic by construction: ``clock`` and ``sleep_fn`` are
    injectable, and :meth:`tick` is callable directly so tests drive
    synthetic time. Production entry is :meth:`run` /
    :meth:`run_forever`.
    """

    def __init__(
        self,
        *,
        registry: SkillRegistry | None = None,
        state_dir: Path | None = None,
        clock: Callable[[], datetime] | None = None,
        tick_interval_seconds: float | None = None,
        node_id: str | None = None,
        lease_ttl_seconds: int = 30,
        sleep_fn: Callable[[float], None] | None = None,
    ) -> None:
        if registry is None:
            from .. import skills as data_skills

            registry = data_skills.bind_default()
        self._registry = registry
        self._state_dir = Path(state_dir) if state_dir else get_user_state_dir()
        self._clock = clock or (lambda: datetime.now(UTC))
        if tick_interval_seconds is None:
            tick_interval_seconds = float(
                os.environ.get(_TICK_INTERVAL_ENV, DEFAULT_TICK_INTERVAL_SECONDS)
            )
        self._tick_interval = float(tick_interval_seconds)
        self._sleep = sleep_fn or time.sleep
        self._stop = threading.Event()
        self._alerts_attached = False

        node = node_id or f"{socket.gethostname()}-{os.getpid()}"
        self._engine_ctx = EngineContext(
            session=pulse_store.session_scope,
            authz=DispatchAuthz(),
            fire_log=pulse_store.SqlFireLog(),
            executor=SkillExecutor(self._registry, state_dir=self._state_dir, logger=_log),
            lease=LeaseManager(node_id=node, ttl_seconds=lease_ttl_seconds),
            now_fn=self._clock,
        )

    # -- alerting -----------------------------------------------------------

    def attach_alerts(self) -> None:
        """Wire dead-letter → HERALD. Idempotent per service instance."""
        if self._alerts_attached:
            return
        pulse_hooks.register(pulse_hooks.ON_DEAD_LETTER, self._on_dead_letter)
        self._alerts_attached = True

    def _on_dead_letter(self, payload: dict[str, Any]) -> None:
        action = payload.get("action", "unknown")
        _herald.publish_event(
            "data.dispatch.dead_letter",
            f"scheduled dispatch dead-lettered: {action} — {payload.get('error')}",
            payload={
                "action": action,
                "schedule_id": payload.get("schedule_id"),
                "error": payload.get("error"),
            },
        )

    # -- BackupPolicy → PULSE projection -------------------------------------

    def ensure_backup_cadences(self, *, now: datetime | None = None) -> dict:
        """Project the persisted BackupPolicy onto PULSE cadences.

        Idempotent: registers missing cadences, reschedules on policy
        edits, pauses when the policy is disabled/absent, resumes on
        re-enable. Returns a small report dict.
        """
        now = now or self._clock()
        policy = load_backup_policy(state_dir=self._state_dir)
        report: dict[str, Any] = {
            "enabled": bool(policy and policy.enabled),
            "errors": [],
            BACKUP_ACTION: None,
            VALIDATE_ACTION: None,
        }

        if policy is None or not policy.enabled:
            for action in (BACKUP_ACTION, VALIDATE_ACTION):
                for row in self._managed_rows(action):
                    if row.state == "active":
                        pulse_api.pause(ScheduleId(row.id), "backup policy disabled", now=now)
                        report[action] = f"paused:{row.id}"
            return report

        errors = validate_policy(policy)
        if errors:
            report["errors"] = errors
            _log.error("backup policy invalid — cadences NOT registered: %s", errors)
            return report

        for action, schedule_str in (
            (BACKUP_ACTION, policy.schedule),
            (VALIDATE_ACTION, policy.validate_schedule),
        ):
            cadence = cadence_for(schedule_str)
            existing = self._managed_rows(action)
            if not existing:
                sid = pulse_api.register(
                    envelope={"action": action, "params": {}},
                    cadence=cadence,
                    action=action,
                    description=f"BackupPolicy: {action} @ {schedule_str}",
                    extension="data_platform",
                    retry_policy={"max_attempts": 1},
                    misfire_policy="fire_once",  # run-once-if-overdue
                    now=now,
                )
                report[action] = f"registered:{sid}"
                continue

            row = existing[0]
            if row.state == "paused":
                pulse_api.resume(ScheduleId(row.id), now=now)
                report[action] = f"resumed:{row.id}"
            if not _cadence_matches(row.cadence_kind, row.cadence_payload, cadence):
                pulse_api.reschedule(ScheduleId(row.id), cadence=cadence, now=now)
                report[action] = f"rescheduled:{row.id}"
            if report[action] is None:
                report[action] = f"unchanged:{row.id}"
        return report

    def _managed_rows(self, action: str) -> list[ScheduleDefinition]:
        """Non-cancelled schedule rows for one of our managed actions."""
        with pulse_store.session_scope() as s:
            rows = (
                s.query(ScheduleDefinition)
                .filter(ScheduleDefinition.action == action)
                .filter(ScheduleDefinition.state != "cancelled")
                .order_by(ScheduleDefinition.created_at)
                .all()
            )
            for r in rows:
                s.expunge(r)
            return rows

    # -- the loop -------------------------------------------------------------

    def tick(self) -> TickReport:
        """One engine tick at the injected clock's now. Test entry point."""
        return engine_tick(self._engine_ctx)

    def run_forever(self, stop_event: threading.Event | None = None) -> None:
        """The production loop: cadence sync, then tick/sleep until stopped."""
        stop = stop_event or self._stop
        self.attach_alerts()
        try:
            report = self.ensure_backup_cadences()
            _log.info("backup cadences synced: %s", report)
        except Exception:  # noqa: BLE001 — the loop must come up regardless
            _log.exception("ensure_backup_cadences failed at startup")
        while not stop.is_set():
            try:
                self.tick()
            except Exception:  # noqa: BLE001 — one bad tick never kills the host
                _log.exception("orchestrator tick failed")
            self._sleep(self._tick_interval)

    def run(self) -> None:  # pragma: no cover — thin alias for the service entry
        self.run_forever()

    def stop(self) -> None:
        self._stop.set()


def _cadence_matches(kind: str, payload: dict, cadence: Cadence) -> bool:
    """Does a stored row's cadence equal the policy's desired cadence?"""
    if kind != cadence.kind:
        return False
    payload = payload or {}
    if cadence.kind == "cron":
        return payload.get("cron_expr") == cadence.cron
    if cadence.kind == "interval":
        return payload.get("interval_seconds") == int(cadence.interval.total_seconds())
    if cadence.kind == "rrule":
        return payload.get("rrule") == cadence.rrule
    return True  # one_shot has no payload


__all__ = [
    "BACKUP_ACTION",
    "DEFAULT_TICK_INTERVAL_SECONDS",
    "VALIDATE_ACTION",
    "DispatchAuthz",
    "OrchestratorService",
]
