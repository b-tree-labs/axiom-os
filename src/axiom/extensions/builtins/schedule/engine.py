# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""PULSE engine — the tick loop.

Per spec-axiom-schedule §3: one loop polls due rows, fires them, retries
per the schedule's ``retry_policy``, and dead-letters on exhaustion. The
same loop handles cron, interval, and one_shot; trigger schedules ship
in PULSE-2.

The engine is clock-agnostic — callers pass ``now`` so test suites can
drive a synthetic clock without ``time.sleep`` or monkey-patching.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Callable, Optional, Protocol

from axiom.extensions.builtins.schedule.lease import LeaseManager


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    """Normalize to tz-aware UTC. Postgres returns aware datetimes; SQLite (the
    test backend) strips tzinfo, so the engine coerces to keep its comparisons
    consistent regardless of the store."""
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


class SessionFactory(Protocol):
    def __call__(self) -> Any: ...


class AuthzClient(Protocol):
    """The shape PULSE consults at fire-time. The real authz primitive
    already implements this via ``decide(envelope, ctx)``.
    """

    def decide(self, envelope: Any) -> Any: ...


class FireLog(Protocol):
    """The idempotency + receipt log. Backed by ``schedule.schedule_fire_log``.

    The contract:

    - ``claim(schedule_id, fire_time_bucket, params_hash, intended_at)``
      returns ``True`` if this engine should fire (inserted a new row)
      or ``False`` if the unique constraint fired (someone already
      claimed it; we read the prior receipt).
    - ``record_outcome(fire_id, outcome, receipt_id, error_summary)``
      writes the terminal state.
    """

    def claim(
        self,
        schedule_id: str,
        fire_time_bucket: int,
        params_hash: str,
        intended_at: datetime,
    ) -> bool: ...

    def record_skipped(self, schedule_id: str, at: datetime, reason: str) -> None: ...

    def record_outcome(
        self,
        schedule_id: str,
        fire_time_bucket: int,
        params_hash: str,
        outcome: str,
        receipt_id: Optional[str],
        error_summary: Optional[str],
    ) -> None: ...


class Executor(Protocol):
    def run(self, action: str, envelope: Any) -> Any: ...


@dataclass
class EngineContext:
    """Runtime dependencies for the tick loop. Tests build a clean one;
    production wires it from ``session_for("schedule")`` + the live
    authz + executor.
    """

    session: SessionFactory
    authz: AuthzClient
    fire_log: FireLog
    executor: Executor
    lease: LeaseManager
    now_fn: Callable[[], datetime]
    window_seconds: int = 60


@dataclass
class TickReport:
    fired: int = 0
    skipped: Optional[str] = None
    errors: list[str] = field(default_factory=list)


def tick(ctx: EngineContext) -> TickReport:
    """One tick of the engine.

    1. Confirm we hold the lease.
    2. Pull every active row with ``next_fire_at <= now``.
    3. For each, claim the idempotency slot, fire, record outcome.
    4. Renew the lease if needed.
    """
    now = ctx.now_fn()

    if not ctx.lease.held(now):
        if ctx.lease.try_acquire(now) is None:
            return TickReport(skipped="not-leader")

    due = _pull_due(ctx, now)

    report = TickReport()
    for defn in due:
        try:
            if _fire_one(defn, now, ctx):
                report.fired += 1
        except Exception as exc:  # pragma: no cover — defensive
            report.errors.append(f"{defn.get('id')}: {exc!r}")

    ctx.lease.maybe_renew(now)
    return report


def _pull_due(ctx: EngineContext, now: datetime) -> list[dict[str, Any]]:
    """Active rows due now, as plain dicts (detached from the session)."""
    from axiom.extensions.builtins.schedule import store

    return [
        {
            "id": r.id,
            "action": r.action,
            "cadence_kind": r.cadence_kind,
            "cadence_payload": r.cadence_payload,
            "retry_policy": r.retry_policy or {},
            "misfire_policy": r.misfire_policy,
            "compliance_window_seconds": r.compliance_window_seconds,
            "compliance_action": r.compliance_action,
            "capability_envelope": r.capability_envelope,
            "next_fire_at": _aware(r.next_fire_at),
        }
        for r in store.pull_due(now)
    ]


def _params_hash(action: str, envelope: Any) -> str:
    import hashlib
    import json

    blob = json.dumps([action, envelope], sort_keys=True, default=repr)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def _allowed(decision: Any) -> bool:
    if isinstance(decision, bool):
        return decision
    for attr in ("allowed", "permit"):
        if hasattr(decision, attr):
            return bool(getattr(decision, attr))
    effect = getattr(decision, "effect", None)
    if effect is not None:
        return str(effect).lower() in ("permit", "allow", "allowed")
    return bool(decision)


def _receipt_id(receipt: Any) -> Optional[str]:
    if receipt is None:
        return None
    if isinstance(receipt, str):
        return receipt
    rid = getattr(receipt, "id", None)
    return str(rid) if rid is not None else None


def _advance(
    defn: dict[str, Any], anchor: datetime, now: datetime, ctx: EngineContext
) -> None:
    """Persist the next fire time. ``anchor`` is the instant the next fire is
    computed from: ``now`` for fire_once/skip (jump past a missed backlog), or
    the missed instant for fire_all (catch up one instant per tick)."""
    from axiom.extensions.builtins.schedule import store
    from axiom.extensions.builtins.schedule.api import cadence_from_payload
    from axiom.extensions.builtins.schedule.cadence import compute_next_fire_at

    cadence = cadence_from_payload(defn["cadence_kind"], defn["cadence_payload"])
    next_at = compute_next_fire_at(cadence, last_fire=anchor, now=now)
    store.set_next_fire_at(defn["id"], next_at)


def _fire_one(defn: dict[str, Any], now: datetime, ctx: EngineContext) -> bool:
    """Fire one due schedule: claim → authz → execute (retry) → record → advance.

    Returns True iff the action executed successfully. Idempotency, authz
    deny, and retry-exhaustion all return False but record the outcome.
    """
    from axiom.extensions.builtins.schedule import hooks

    schedule_id = defn["id"]
    intended = defn["next_fire_at"] or now
    bucket = int(intended.timestamp()) // ctx.window_seconds
    envelope = defn.get("capability_envelope")
    phash = _params_hash(defn["action"], envelope)
    payload = {
        "schedule_id": schedule_id,
        "action": defn["action"],
        "intended_fire_at": intended,
        "envelope": envelope,
    }

    # Blackout: suppress fires inside an active maintenance / closure window;
    # the schedule resumes after it.
    from axiom.extensions.builtins.schedule import blackout
    if blackout.in_blackout(now):
        _advance(defn, now, now, ctx)
        hooks.emit(hooks.ON_FAILURE, {**payload, "reason": "blackout"})
        return False

    # Misfire handling: an instant more than one window late was missed while the
    # engine was down. fire_all catches up (anchor on the missed instant); skip
    # drops it; fire_once fires this one and jumps to the future.
    policy = defn.get("misfire_policy", "fire_once")
    missed = intended < now - timedelta(seconds=ctx.window_seconds)
    anchor = intended if policy == "fire_all" else now

    if policy == "skip" and missed:
        _advance(defn, now, now, ctx)
        hooks.emit(hooks.ON_FAILURE, {**payload, "reason": "misfire_skip"})
        return False

    # Idempotency: only the engine that wins the unique-constraint claim fires.
    if not ctx.fire_log.claim(schedule_id, bucket, phash, intended):
        return False

    # pre_fire gate — precondition / allocation veto (fail-closed).
    proceed, reason = hooks.gate(hooks.PRE_FIRE, payload)
    if not proceed:
        ctx.fire_log.record_outcome(schedule_id, bucket, phash, "failed", None, reason)
        hooks.emit(hooks.ON_FAILURE, {**payload, "reason": reason})
        _advance(defn, anchor, now, ctx)
        return False

    # Authorization at fire time — a deny is recorded, never executed.
    if not _allowed(ctx.authz.decide(envelope)):
        ctx.fire_log.record_outcome(
            schedule_id, bucket, phash, "failed", None, "authz_denied"
        )
        hooks.emit(hooks.ON_FAILURE, {**payload, "reason": "authz_denied"})
        _advance(defn, anchor, now, ctx)
        return False

    # Compliance window: a fire later than its window is a deviation
    # (out_of_window), distinct from dead-letter. skip = don't fire late;
    # flag = fire but record the deviation for the audit trail.
    cw = defn.get("compliance_window_seconds")
    lateness = (now - intended).total_seconds()
    out_of_window = cw is not None and lateness > cw
    if out_of_window:
        hooks.emit(hooks.ON_FAILURE, {**payload, "reason": "compliance_window_miss",
                                      "lateness_seconds": lateness})
        if defn.get("compliance_action", "flag") == "skip":
            ctx.fire_log.record_outcome(
                schedule_id, bucket, phash, "out_of_window", None, "compliance_skip"
            )
            _advance(defn, anchor, now, ctx)
            return False

    max_attempts = int((defn.get("retry_policy") or {}).get("max_attempts", 1))
    last_err: Optional[str] = None
    for attempt in range(1, max(1, max_attempts) + 1):
        try:
            receipt = ctx.executor.run(defn["action"], envelope)
            ctx.fire_log.record_outcome(
                schedule_id, bucket, phash,
                "out_of_window" if out_of_window else "success",
                _receipt_id(receipt), None,
            )
            hooks.emit(hooks.ON_SUCCESS, {**payload, "receipt": _receipt_id(receipt)})
            _advance(defn, anchor, now, ctx)
            return True
        except Exception as exc:  # noqa: BLE001 — retried, then dead-lettered
            last_err = repr(exc)
            hooks.emit(hooks.ON_FAILURE, {**payload, "error": last_err, "attempt": attempt})

    ctx.fire_log.record_outcome(
        schedule_id, bucket, phash, "dead_letter", None, last_err
    )
    hooks.emit(hooks.ON_DEAD_LETTER, {**payload, "error": last_err})
    _advance(defn, anchor, now, ctx)
    return False


__all__ = [
    "AuthzClient",
    "EngineContext",
    "Executor",
    "FireLog",
    "SessionFactory",
    "TickReport",
    "tick",
]
