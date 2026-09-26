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
from datetime import UTC, datetime, timedelta
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
FRESHNESS_ACTION = "data.ingest_freshness"
REFRESH_ACTION = "data.refresh"

#: When the freshness sweep runs. Daily is the right order of magnitude: the
#: failure it exists to catch is a partner stream going quiet for MONTHS, and a
#: check that runs more often than the problem develops only adds noise.
DEFAULT_FRESHNESS_SCHEDULE = "0 9 * * *"

# Two staleness questions, deliberately not merged into one. `data.refresh`
# asks whether the CDC pull RAN for a connector; `data.ingest_freshness` asks
# whether rows actually LANDED for a stream. A connector can refresh on time
# and deliver nothing, which is the case that looks healthiest and matters
# most, so collapsing them would lose exactly the failure worth catching.

DEFAULT_TICK_INTERVAL_SECONDS = 30.0
#: Default CDC-refresh cadence for an enrolled connector. Override or disable per
#: connector via ``params.refresh_schedule`` / ``params.refresh_enabled=false``.
DEFAULT_REFRESH_SCHEDULE = "@daily"
#: A connector with no successful refresh within this window is stale. Two days
#: gives a daily cadence one missed run of slack. Override per connector via
#: ``params.freshness_window_hours``.
DEFAULT_FRESHNESS_WINDOW_HOURS = 48.0
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

    def ensure_freshness_cadence(self, *, now: datetime | None = None) -> dict:
        """Register the daily ingest-freshness sweep on PULSE.

        **Deliberately not opt-in.** The defect this closes is that a partner
        stream went quiet for four months and nothing said so; a check an
        operator has to remember to enable is the same defect with an extra
        step. It is read-only, so the cost of it being on is one query a day.

        ``alert=True`` is passed HERE and nowhere else. The skill is silent when
        a human runs it — inspecting freshness should not page anyone — and
        raises HERALD only on the scheduled sweep. That asymmetry is the whole
        reason the cadence carries params at all.

        Idempotent, like its backup sibling: registers when missing, reschedules
        when the cadence changed, resumes if someone paused it.
        """
        now = now or self._clock()
        report: dict[str, Any] = {FRESHNESS_ACTION: None}
        cadence = cadence_for(DEFAULT_FRESHNESS_SCHEDULE)
        existing = self._managed_rows(FRESHNESS_ACTION)

        if not existing:
            sid = pulse_api.register(
                envelope={"action": FRESHNESS_ACTION, "params": {"alert": True}},
                cadence=cadence,
                action=FRESHNESS_ACTION,
                description=(
                    f"ingest freshness sweep @ {DEFAULT_FRESHNESS_SCHEDULE} "
                    "(alerts on newly stale streams)"
                ),
                extension="data_platform",
                # One attempt. A retry would re-run a read-only sweep whose
                # finding has not changed, and the dedup key would suppress the
                # second alert anyway — so a retry buys nothing and hides the
                # fact that the sweep failed.
                retry_policy={"max_attempts": 1},
                misfire_policy="fire_once",
                now=now,
            )
            report[FRESHNESS_ACTION] = f"registered:{sid}"
            return report

        row = existing[0]
        if row.state == "paused":
            pulse_api.resume(ScheduleId(row.id), now=now)
            report[FRESHNESS_ACTION] = f"resumed:{row.id}"
        if not _cadence_matches(row.cadence_kind, row.cadence_payload, cadence):
            pulse_api.reschedule(ScheduleId(row.id), cadence=cadence, now=now)
            report[FRESHNESS_ACTION] = f"rescheduled:{row.id}"
        if report[FRESHNESS_ACTION] is None:
            report[FRESHNESS_ACTION] = f"unchanged:{row.id}"
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

    # -- connector CDC refresh → PULSE projection ----------------------------

    def ensure_refresh_cadences(self, *, now: datetime | None = None) -> dict:
        """Project enrolled connectors onto daily CDC-refresh cadences.

        Every enrolled connector gets a ``data.refresh`` cadence (default
        ``@daily``) so incremental Box/RAG ingest runs on a schedule instead of
        by hand — the gap that let ! Operations go a month stale. Idempotent:
        registers missing cadences, reschedules on a changed
        ``params.refresh_schedule``, pauses a connector that is removed or sets
        ``params.refresh_enabled=false``, resumes on re-enable. One action serves
        every connector; the connector is carried in the fire-time envelope
        (``capability_envelope.params.connector``) so ``data.refresh`` receives it.
        """
        from ..agents.plinth.connectors import list_connectors

        now = now or self._clock()
        report: dict[str, Any] = {"cadences": {}, "errors": []}

        desired: dict[str, tuple[Cadence, str]] = {}
        for c in list_connectors(state_dir=self._state_dir):
            if c.params.get("refresh_enabled", "true").strip().lower() == "false":
                continue
            schedule_str = c.params.get("refresh_schedule", DEFAULT_REFRESH_SCHEDULE)
            try:
                desired[c.name] = (cadence_for(schedule_str), schedule_str)
            except Exception as exc:  # noqa: BLE001 — one bad string never sinks the rest
                report["errors"].append(f"{c.name}: bad refresh_schedule {schedule_str!r}: {exc}")

        existing = self._refresh_rows_by_connector()

        for name, (cadence, schedule_str) in desired.items():
            rows = existing.get(name, [])
            if not rows:
                sid = pulse_api.register(
                    envelope={"action": REFRESH_ACTION, "params": {"connector": name}},
                    cadence=cadence,
                    action=REFRESH_ACTION,
                    description=f"CDC refresh: {name} @ {schedule_str}",
                    extension="data_platform",
                    retry_policy={"max_attempts": 2},
                    misfire_policy="fire_once",  # run-once-if-overdue; the next tick catches up
                    now=now,
                )
                report["cadences"][name] = f"registered:{sid}"
                continue
            row = rows[0]
            outcome = None
            if row.state == "paused":
                pulse_api.resume(ScheduleId(row.id), now=now)
                outcome = f"resumed:{row.id}"
            if not _cadence_matches(row.cadence_kind, row.cadence_payload, cadence):
                pulse_api.reschedule(ScheduleId(row.id), cadence=cadence, now=now)
                outcome = f"rescheduled:{row.id}"
            report["cadences"][name] = outcome or f"unchanged:{row.id}"

        # Pause cadences for connectors no longer enrolled or now disabled.
        for name, rows in existing.items():
            if name in desired:
                continue
            for row in rows:
                if row.state == "active":
                    pulse_api.pause(
                        ScheduleId(row.id), "connector removed or refresh disabled", now=now
                    )
                    report["cadences"][name] = f"paused:{row.id}"
        return report

    def _refresh_rows_by_connector(self) -> dict[str, list[ScheduleDefinition]]:
        """Non-cancelled ``data.refresh`` rows grouped by their envelope connector."""
        out: dict[str, list[ScheduleDefinition]] = {}
        with pulse_store.session_scope() as s:
            rows = (
                s.query(ScheduleDefinition)
                .filter(ScheduleDefinition.action == REFRESH_ACTION)
                .filter(ScheduleDefinition.state != "cancelled")
                .order_by(ScheduleDefinition.created_at)
                .all()
            )
            for r in rows:
                env = r.capability_envelope or {}
                conn = str((env.get("params") or {}).get("connector") or "")
                s.expunge(r)
                if conn:
                    out.setdefault(conn, []).append(r)
        return out

    # -- ingest freshness (never-stale detector) -----------------------------

    def refresh_freshness_report(self, *, now: datetime | None = None) -> dict:
        """Detect connectors whose CDC ingest has gone stale, and alert.

        For each connector with an active ``data.refresh`` cadence, the newest
        ``success`` in the fire-log must be within its freshness window (default
        48h). No success within the window → HERALD ``data.refresh.stale`` (one
        per connector, deduped). A just-registered cadence gets a grace period —
        it can't be stale before it has had a window's chance to fire — so a
        fresh enrolment never false-alarms. This is the detector that would have
        caught ! Operations going dark for a month.
        """
        from ..agents.plinth.connectors import list_connectors

        now = now or self._clock()
        windows: dict[str, float] = {}
        for c in list_connectors(state_dir=self._state_dir):
            try:
                windows[c.name] = float(
                    c.params.get("freshness_window_hours", DEFAULT_FRESHNESS_WINDOW_HOURS)
                )
            except (TypeError, ValueError):
                windows[c.name] = DEFAULT_FRESHNESS_WINDOW_HOURS

        report: dict[str, Any] = {"connectors": {}, "stale": []}
        for conn, rows in self._refresh_rows_by_connector().items():
            active = [r for r in rows if r.state == "active"]
            if not active:
                continue
            window = timedelta(hours=windows.get(conn, DEFAULT_FRESHNESS_WINDOW_HOURS))
            last_success = _as_utc(self._last_refresh_success([r.id for r in active]))
            fresh = last_success is not None and (now - last_success) <= window
            ages = [now - _as_utc(r.created_at) for r in active if r.created_at]
            in_grace = last_success is None and ages and min(ages) < window
            stale = not fresh and not in_grace
            report["connectors"][conn] = {
                "last_success": last_success.isoformat() if last_success else None,
                "window_hours": window.total_seconds() / 3600,
                "stale": stale,
            }
            if stale:
                report["stale"].append(conn)
                hours = int(window.total_seconds() // 3600)
                _herald.publish_event(
                    "data.refresh.stale",
                    f"connector {conn!r} ingest is stale: no successful CDC refresh within {hours}h",
                    dedup_key=f"data.refresh.stale:{conn}",
                    payload={
                        "connector": conn,
                        "last_success": report["connectors"][conn]["last_success"],
                        "window_hours": report["connectors"][conn]["window_hours"],
                    },
                )
        return report

    @staticmethod
    def _last_refresh_success(schedule_ids: list[str]) -> datetime | None:
        """Newest successful fire across a connector's refresh cadence rows."""
        if not schedule_ids:
            return None
        from sqlalchemy import func

        from axiom.extensions.builtins.schedule.db_models import ScheduleFireLog

        with pulse_store.session_scope() as s:
            return (
                s.query(func.max(ScheduleFireLog.intended_fire_at))
                .filter(ScheduleFireLog.schedule_id.in_(schedule_ids))
                .filter(ScheduleFireLog.outcome == "success")
                .scalar()
            )

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
        # Each cadence syncs in its OWN try. One failing to project must not
        # take the others down with it — they are independent, and a broken
        # backup policy is not a reason to stop watching partner data.
        try:
            report = self.ensure_freshness_cadence()
            _log.info("freshness cadence synced: %s", report)
        except Exception:  # noqa: BLE001 — the loop must come up regardless
            _log.exception("ensure_freshness_cadence failed at startup")
        try:
            refresh_report = self.ensure_refresh_cadences()
            _log.info("refresh cadences synced: %s", refresh_report)
        except Exception:  # noqa: BLE001 — the loop must come up regardless
            _log.exception("ensure_refresh_cadences failed at startup")
        try:
            fresh_report = self.refresh_freshness_report()
            if fresh_report["stale"]:
                _log.warning("stale connectors at startup: %s", fresh_report["stale"])
        except Exception:  # noqa: BLE001 — the loop must come up regardless
            _log.exception("refresh_freshness_report failed at startup")
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


def _as_utc(dt: datetime | None) -> datetime | None:
    """Coerce a possibly-naive stored timestamp to UTC-aware. Postgres round-trips
    tz; SQLite (tests) drops it, so normalize before any datetime arithmetic."""
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


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
    "DEFAULT_FRESHNESS_SCHEDULE",
    "DEFAULT_REFRESH_SCHEDULE",
    "DEFAULT_TICK_INTERVAL_SECONDS",
    "FRESHNESS_ACTION",
    "REFRESH_ACTION",
    "VALIDATE_ACTION",
    "DispatchAuthz",
    "OrchestratorService",
]
