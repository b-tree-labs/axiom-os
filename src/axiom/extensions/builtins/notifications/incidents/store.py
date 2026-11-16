# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Durable incidents for the incident stage (ADR-249 D2, ADR-252).

Senders (the scheduler's dead-letter hook, health checks, freshness sweeps)
run in other processes than the stage's tick, so the stage keeps nothing it
needs in memory: every decision reads and writes this store.

ADR-052: storage goes through ``session_for("notifications")``. No engine is
constructed here outside tests, no schema is named, and table names stay
unqualified.
"""

from __future__ import annotations

from collections.abc import Iterable
from contextlib import contextmanager
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session as _SASession

from ..db_models import IncidentRow
from .model import Attempt, Incident, Unmet


def _aware(value: datetime | None) -> datetime | None:
    """SQLite returns naive datetimes; restore UTC so stamps compare."""
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _to_incident(row: IncidentRow) -> Incident:
    return Incident(
        id=row.id,
        fingerprint=row.fingerprint,
        area=row.area,
        kind=row.kind,
        action=row.action,
        error_class=row.error_class,
        priority=row.priority,  # type: ignore[arg-type]
        owner_agent=row.owner_agent or "",
        summary=row.summary or "",
        unmet=tuple(Unmet(**u) for u in (row.unmet or [])),
        subjects=list(row.subjects or []),
        first_seen=_aware(row.first_seen),  # type: ignore[arg-type]
        last_seen=_aware(row.last_seen),  # type: ignore[arg-type]
        occurrences=int(row.occurrences or 0),
        remedy=row.remedy or "",
        state=row.state,  # type: ignore[arg-type]
        hold_until=_aware(row.hold_until),
        attempts=[
            Attempt(
                remedy=a["remedy"],
                at=_aware(datetime.fromisoformat(a["at"])),  # type: ignore[arg-type]
                ok=bool(a["ok"]),
                outcome=a.get("outcome", ""),
            )
            for a in (row.attempts or [])
        ],
        evidence_refs=list(row.evidence_refs or []),
        reopened=int(row.reopened or 0),
        notified_at=_aware(row.notified_at),
        snoozed_until=_aware(row.snoozed_until),
        notes=list(row.notes or []),
    )


def _fill(row: IncidentRow, inc: Incident, closed_at: datetime | None) -> None:
    row.fingerprint = inc.fingerprint
    row.area = inc.area
    row.kind = inc.kind
    row.action = inc.action
    row.error_class = inc.error_class
    row.priority = inc.priority
    row.owner_agent = inc.owner_agent
    row.summary = inc.summary
    row.remedy = inc.remedy
    row.unmet = [asdict(u) for u in inc.unmet]
    row.subjects = list(inc.subjects)
    row.first_seen = inc.first_seen
    row.last_seen = inc.last_seen
    row.occurrences = inc.occurrences
    row.state = inc.state
    row.hold_until = inc.hold_until
    row.attempts = [
        {"remedy": a.remedy, "at": a.at.isoformat(), "ok": a.ok, "outcome": a.outcome}
        for a in inc.attempts
    ]
    row.evidence_refs = list(inc.evidence_refs)
    row.reopened = inc.reopened
    row.notified_at = inc.notified_at
    row.snoozed_until = inc.snoozed_until
    row.notes = list(inc.notes)
    row.closed_at = closed_at
    row.updated_at = datetime.now(UTC)


class IncidentStore:
    """Incident persistence. One row per fingerprint.

    Beside the :class:`Incident` itself each row keeps its ``delivery``: the
    recipient, classification and intent the held notification was sent with,
    so the stage delivers it later exactly as the sender asked."""

    def __init__(self, engine=None) -> None:
        #: Injected only by tests; production goes through ``session_for``.
        self._engine = engine

    @contextmanager
    def _session(self):
        if self._engine is not None:
            with _SASession(self._engine) as session:
                yield session
            return
        from axiom.infra.db import session_for

        with session_for("notifications") as session:
            yield session

    # ── reads ────────────────────────────────────────────────────────────
    def get(self, incident_id: str) -> Incident | None:
        with self._session() as s:
            row = s.get(IncidentRow, incident_id)
            return _to_incident(row) if row is not None else None

    def delivery(self, incident_id: str) -> dict[str, Any]:
        with self._session() as s:
            row = s.get(IncidentRow, incident_id)
            return dict(row.delivery or {}) if row is not None else {}

    def by_fingerprint(self, fingerprint: str) -> Incident | None:
        with self._session() as s:
            row = s.scalars(
                select(IncidentRow).where(IncidentRow.fingerprint == fingerprint)
            ).first()
            return _to_incident(row) if row is not None else None

    def in_states(self, states: Iterable[str]) -> list[Incident]:
        with self._session() as s:
            rows = s.scalars(
                select(IncidentRow)
                .where(IncidentRow.state.in_(list(states)))
                .order_by(IncidentRow.first_seen)
            ).all()
            return [_to_incident(r) for r in rows]

    def closed_since(self, since: datetime) -> list[Incident]:
        """Incidents healed or resolved at or after ``since``."""
        with self._session() as s:
            rows = s.scalars(
                select(IncidentRow)
                .where(IncidentRow.closed_at.is_not(None))
                .where(IncidentRow.closed_at >= since)
                .order_by(IncidentRow.closed_at)
            ).all()
            return [_to_incident(r) for r in rows]

    # ── writes ───────────────────────────────────────────────────────────
    def insert(self, inc: Incident, *, delivery: dict[str, Any] | None = None) -> None:
        """Add a new incident. Raises ``IntegrityError`` if its fingerprint
        already has a row (another process opened it first)."""
        with self._session() as s:
            row = IncidentRow(id=inc.id, delivery=dict(delivery or {}))
            _fill(row, inc, None)
            s.add(row)
            s.commit()

    def save(
        self,
        inc: Incident,
        *,
        closed_at: datetime | None = None,
        delivery: dict[str, Any] | None = None,
    ) -> None:
        """Write an incident's current state. ``closed_at`` is kept from the
        row while the incident stays closed, and cleared when it reopens."""
        with self._session() as s:
            row = s.get(IncidentRow, inc.id)
            if row is None:
                row = IncidentRow(id=inc.id, delivery={})
                s.add(row)
                previous = None
            else:
                previous = row.closed_at
            if delivery is not None:
                row.delivery = dict(delivery)
            closed = inc.state in ("healed", "resolved")
            _fill(row, inc, (closed_at or previous) if closed else None)
            s.commit()


__all__ = ["IncidentStore"]
