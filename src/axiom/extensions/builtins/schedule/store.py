# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Persistence helpers for PULSE — the seam between the engine/API and the
``schedule`` Postgres schema.

Production binds a session provider that wraps
``axiom.infra.db.session_for('schedule')`` (ADR-052). Tests bind a provider
that yields a SQLite session, so the fire loop is driven without a live
Postgres. The provider is the *only* injection point — engine, API, and the
consumer seam all go through ``session_scope()``.
"""

from __future__ import annotations

import contextlib
from datetime import datetime
from typing import Any, Callable, ContextManager, Iterator, Optional

from sqlalchemy.exc import IntegrityError

from axiom.extensions.builtins.schedule.db_models import (
    ScheduleDefinition,
    ScheduleFireLog,
)


def _default_provider() -> ContextManager[Any]:
    # Lazy import so unit tests that bind their own provider never touch the
    # Postgres-backed db module.
    from axiom.infra.db import session_for

    return session_for("schedule")


_provider: Callable[[], ContextManager[Any]] = _default_provider


def set_provider(provider: Callable[[], ContextManager[Any]]) -> None:
    """Bind the session provider (tests use this to inject SQLite)."""
    global _provider
    _provider = provider


def reset_provider() -> None:
    global _provider
    _provider = _default_provider


@contextlib.contextmanager
def session_scope() -> Iterator[Any]:
    """Yield a Session from the bound provider. Caller commits."""
    with _provider() as session:
        yield session


def _fire_id(schedule_id: str, bucket: int, params_hash: str) -> str:
    return f"{schedule_id}:{bucket}:{params_hash}"


class SqlFireLog:
    """The idempotency + receipt log, backed by ``schedule_fire_log``.

    ``claim`` returns True if this engine inserted the row (it should fire) or
    False if the unique constraint rejected it (someone already claimed this
    exact instant — idempotent skip, no re-execution).
    """

    def claim(
        self,
        schedule_id: str,
        fire_time_bucket: int,
        params_hash: str,
        intended_at: datetime,
    ) -> bool:
        row = ScheduleFireLog(
            id=_fire_id(schedule_id, fire_time_bucket, params_hash),
            schedule_id=schedule_id,
            fire_time_bucket=fire_time_bucket,
            params_hash=params_hash,
            intended_fire_at=intended_at,
            started_at=intended_at,
            attempt=1,
            outcome="pending",
        )
        try:
            with session_scope() as s:
                s.add(row)
                s.commit()
            return True
        except IntegrityError:
            # Unique (schedule_id, fire_time_bucket, params_hash) — already claimed.
            return False

    def record_skipped(self, schedule_id: str, at: datetime, reason: str) -> None:
        self._update(schedule_id, "failed", error=reason)

    def record_outcome(
        self,
        schedule_id: str,
        fire_time_bucket: int,
        params_hash: str,
        outcome: str,
        receipt_id: Optional[str],
        error_summary: Optional[str],
    ) -> None:
        self._update(
            schedule_id,
            outcome,
            receipt=receipt_id,
            error=error_summary,
            bucket=fire_time_bucket,
            params_hash=params_hash,
        )

    def _update(
        self,
        schedule_id: str,
        outcome: str,
        *,
        receipt: Optional[str] = None,
        error: Optional[str] = None,
        bucket: Optional[int] = None,
        params_hash: Optional[str] = None,
        attempt: Optional[int] = None,
        finished_at: Optional[datetime] = None,
    ) -> None:
        with session_scope() as s:
            q = s.query(ScheduleFireLog).filter(
                ScheduleFireLog.schedule_id == schedule_id
            )
            if bucket is not None:
                q = q.filter(ScheduleFireLog.fire_time_bucket == bucket)
            if params_hash is not None:
                q = q.filter(ScheduleFireLog.params_hash == params_hash)
            row = q.order_by(ScheduleFireLog.intended_fire_at.desc()).first()
            if row is None:
                return
            row.outcome = outcome
            if receipt is not None:
                row.receipt_fragment_id = receipt
            if error is not None:
                row.error_summary = error
            if attempt is not None:
                row.attempt = attempt
            if finished_at is not None:
                row.finished_at = finished_at
            s.commit()


def pull_due(now: datetime) -> list[ScheduleDefinition]:
    """Active rows whose ``next_fire_at`` is due. Detached copies for the loop."""
    with session_scope() as s:
        rows = (
            s.query(ScheduleDefinition)
            .filter(ScheduleDefinition.state == "active")
            .filter(ScheduleDefinition.next_fire_at.isnot(None))
            .filter(ScheduleDefinition.next_fire_at <= now)
            .all()
        )
        for r in rows:
            s.expunge(r)
        return rows


def set_next_fire_at(schedule_id: str, next_at: Optional[datetime]) -> None:
    with session_scope() as s:
        row = s.get(ScheduleDefinition, schedule_id)
        if row is not None:
            row.next_fire_at = next_at
            s.commit()


__all__ = [
    "SqlFireLog",
    "pull_due",
    "reset_provider",
    "session_scope",
    "set_next_fire_at",
    "set_provider",
]
