# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A notifications inbox that outlives the process.

`InMemoryInboxStore` was the only implementation, so an alert written by a
monitor in one process was invisible to a chat running in another — which is
every real deployment. The Protocol's own docstring has always promised
"in-memory + Postgres implementations conform"; this is that one.

ADR-052: storage goes through ``session_for("notifications")``. No engine is
constructed here, no schema is named, and table names stay unqualified — the
provider sets ``search_path`` per connection.
"""

from __future__ import annotations

import logging
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as _SASession

from .db_models import DedupLog, NotificationsInbox
from .inbox import InboxQuery, InboxRow

_LOG = logging.getLogger(__name__)

DEDUP_PRIMITIVE = "notifications"

# One day. A monitor on a nightly timer re-observes a standing condition once
# per run, so the window that makes it quiet is the one that spans a run.
DEFAULT_DEDUP_TTL_SECONDS = 86_400


@dataclass(frozen=True)
class AlertWrite:
    """What happened when a monitor asked for an alert to be recorded.

    ``deduplicated`` is the part a monitor author needs: their rule fired, and
    the reason nobody was woken is that this exact alert already stands. That is
    a different fact from "the rule stayed quiet", and a caller that cannot tell
    them apart cannot report honestly.
    """

    row_id: str
    receipt_id: str
    deduplicated: bool = False
    verified: bool = False
    verification: str = ""
    """Evidence the alert is readable, not merely that the write returned.

    A receipt that says "delivered" because a function did not raise is the
    failure this whole path was rebuilt around. The alert is read back through
    the reader's own query after writing, so the receipt carries what was
    established rather than what was assumed."""


def _as_row(record: NotificationsInbox, actor: str = "") -> InboxRow:
    """Map a table record onto the store's public row type."""
    return InboxRow(
        id=record.id,
        receipt_id=record.receipt_id or "",
        recipient=record.recipient,
        classification=record.classification,
        priority=record.priority,
        summary=record.summary,
        read_at=_aware(record.read_at),
        acknowledged_at=_aware(record.acknowledged_at),
        acknowledged_by=record.acknowledged_by or "",
        muted=bool(record.muted),
        link=record.body_ref or "",
        actor=actor,
        created_at=_aware(record.created_at) or datetime.now(UTC),
    )


def _aware(value: datetime | None) -> datetime | None:
    """Re-attach UTC to a naive timestamp.

    SQLite hands back naive datetimes even for timezone-aware columns. Surfaces
    compare these across machines, and a naive stamp cannot be ordered against
    one from another host, so the tzinfo is restored on read rather than left
    to whoever compares them next.
    """
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


class DatabaseInboxStore:
    """Postgres-backed inbox. Conforms to :class:`~.inbox.InboxStore`."""

    durable = True

    def __init__(self, engine=None) -> None:
        #: Injected only by tests; production goes through `session_for`.
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

    # ── writes ───────────────────────────────────────────────────────────
    def write(
        self,
        *,
        recipient: str,
        receipt_id: str,
        classification: str,
        priority: str,
        summary: str,
    ) -> str:
        row_id = str(uuid.uuid4())
        with self._session() as session:
            session.add(
                NotificationsInbox(
                    id=row_id,
                    receipt_id=receipt_id,
                    recipient=recipient,
                    classification=classification,
                    priority=priority,
                    summary=summary,
                    created_at=datetime.now(UTC),
                )
            )
            session.commit()
        return row_id

    def mark_read(self, *, row_id: str) -> None:
        """Idempotent, and harmless on a row that does not exist."""
        with self._session() as session:
            record = session.get(NotificationsInbox, row_id)
            if record is None:
                return
            if record.read_at is None:
                record.read_at = datetime.now(UTC)
                session.commit()

    def write_alert(
        self,
        *,
        recipient: str,
        summary: str,
        priority: str = "normal",
        classification: str = "internal",
        actor: str = "@monitor:local",
        link: str = "",
        dedup_key: str = "",
        dedup_ttl_seconds: int = DEFAULT_DEDUP_TTL_SECONDS,
    ) -> AlertWrite:
        """Record an alert: its delivery receipt and its inbox row, together.

        An inbox row exists BECAUSE a receipt does — the foreign key says so —
        and nothing else in this extension persists receipts, so a caller that
        writes only the inbox row violates that constraint. Both are written in
        one transaction: an inbox entry with no receipt is an alert with no
        provenance, which is exactly what an operator cannot act on.

        This is the path a monitor uses. `send()` has its own in-memory
        pipeline with channel routing; this is the durable floor beneath it.

        ``dedup_key`` makes that floor idempotent. A monitor runs on a timer, so
        a condition that stays true is re-observed every run; without a key the
        operator gets the same alert nightly until they mute the monitor. With
        one, the first write stands and later runs report back the alert that
        already exists. The key is scoped to the ``actor``, so two monitors that
        happen to choose the same string never swallow each other's alerts.
        Omit it and nothing is suppressed — dedup is opt-in.
        """
        from .db_models import DeliveryReceipt

        if dedup_key:
            standing = self._standing_alert(actor=actor, dedup_key=dedup_key)
            if standing is not None:
                return standing

        receipt_id = f"rcpt-{uuid.uuid4().hex[:12]}"
        row_id = str(uuid.uuid4())
        now = datetime.now(UTC)
        try:
            with self._session() as session:
                session.add(
                    DeliveryReceipt(
                        id=receipt_id,
                        envelope_json={"summary": summary},
                        intent="alert",
                        actor=actor,
                        recipient=recipient,
                        classification=classification,
                        priority=priority,
                        outcome="delivered",
                        correlation_id=f"corr-{uuid.uuid4().hex[:12]}",
                        created_at=now,
                    )
                )
                session.add(
                    NotificationsInbox(
                        id=row_id,
                        receipt_id=receipt_id,
                        recipient=recipient,
                        classification=classification,
                        priority=priority,
                        summary=summary,
                        body_ref=link or None,
                        created_at=now,
                    )
                )
                if dedup_key:
                    session.add(
                        DedupLog(
                            primitive=DEDUP_PRIMITIVE,
                            actor=actor,
                            dedup_key=dedup_key,
                            receipt_id=receipt_id,
                            expires_at=now + timedelta(seconds=dedup_ttl_seconds),
                        )
                    )
                session.commit()
        except IntegrityError:
            # Two runs of the same monitor raced — timers overlap, and a retry
            # after a slow write looks identical from here. The primary key on
            # (primitive, actor, dedup_key) is what actually decides the winner;
            # the check above is only the fast path. Report the one that landed.
            standing = self._standing_alert(actor=actor, dedup_key=dedup_key)
            if standing is not None:
                return standing
            raise
        # Go and look. A read-back through the reader's own path is the
        # cheapest honest evidence available at write time.
        from axiom.extensions.builtins.notifications.verification import (
            verify_readback,
        )

        check = verify_readback(self, row_id=row_id, recipient=recipient)
        return AlertWrite(
            row_id=row_id,
            receipt_id=receipt_id,
            deduplicated=False,
            verified=check.verified,
            verification=check.detail,
        )

    def _standing_alert(self, *, actor: str, dedup_key: str) -> AlertWrite | None:
        """The unexpired alert this (actor, key) already wrote, if any.

        An expired row is cleared rather than reused: a condition that returns
        after the window is news again, and leaving the stale row would suppress
        it forever.
        """
        now = datetime.now(UTC)
        with self._session() as session:
            logged = session.get(
                DedupLog, (DEDUP_PRIMITIVE, actor, dedup_key)
            )
            if logged is None:
                return None
            expires_at = _aware(logged.expires_at)
            if expires_at is not None and expires_at <= now:
                session.delete(logged)
                session.commit()
                return None
            receipt_id = logged.receipt_id
            row = session.scalars(
                select(NotificationsInbox).where(
                    NotificationsInbox.receipt_id == receipt_id
                )
            ).first()
            if row is None:
                # The dedup row outlived the alert it points at (a purged inbox).
                # Suppressing against an alert nobody can read would silence the
                # monitor for nothing, so let the next write through.
                session.delete(logged)
                session.commit()
                return None
            return AlertWrite(
                row_id=row.id, receipt_id=receipt_id, deduplicated=True
            )

    def acknowledge(self, *, row_id: str, by: str) -> bool:
        """Record that a person took responsibility for an alert.

        ONLY the recipient may acknowledge their own alert. There is no column
        for "who acknowledged", and that is fine precisely because of this
        rule: the row's recipient IS the acknowledger, so the record is
        unambiguous. Allowing anyone to ack would make `acknowledged_at` mean
        "somebody, at some point", which is not a fact anyone can act on.

        Idempotent, and the timestamp does NOT move on a second call — when an
        alert was first taken up is the auditable fact.

        Returns whether this call performed the acknowledgement.
        """
        with self._session() as session:
            record = session.get(NotificationsInbox, row_id)
            if record is None:
                return False
            if record.recipient != by:
                # Not an error the caller can fix by retrying, and not silent:
                # the CLI turns this into a message naming the mismatch.
                raise PermissionError(
                    f"{by!r} cannot acknowledge an alert addressed to "
                    f"{record.recipient!r}"
                )
            if record.acknowledged_at is not None:
                return False
            now = datetime.now(UTC)
            record.acknowledged_at = now
            # Recorded, not implied. The recipient happens to be the only
            # permitted answerer today; that is an AUTHORITY fact and belongs
            # to GUARD. This column is the RECORD, and it must survive the day
            # a group recipient makes the two diverge.
            record.acknowledged_by = by
            # Acknowledging implies reading it; an unread-but-acknowledged row
            # would keep re-arriving in chat after the person had dealt with it.
            if record.read_at is None:
                record.read_at = now
            session.commit()
            return True

    # ── reads ────────────────────────────────────────────────────────────
    def query(self, q: InboxQuery) -> list[InboxRow]:
        if not (q.recipient or "").strip():
            # An unidentified reader has no inbox. NOT "every inbox" — that
            # would make the recipient filter a formality.
            return []
        # Joined to the receipt for the ACTOR. Who reported an alert is the
        # first thing an operator asks and the row itself does not carry it —
        # an outer join so a row whose receipt is missing still appears rather
        # than vanishing from the list it belongs in.
        from .db_models import DeliveryReceipt

        statement = (
            select(NotificationsInbox, DeliveryReceipt.actor)
            .join(
                DeliveryReceipt,
                DeliveryReceipt.id == NotificationsInbox.receipt_id,
                isouter=True,
            )
            .where(NotificationsInbox.recipient == q.recipient)
            .order_by(NotificationsInbox.created_at.desc())
        )
        if q.unread_only:
            statement = statement.where(NotificationsInbox.read_at.is_(None))
        with self._session() as session:
            records = session.execute(statement).all()
        rows = [_as_row(record, actor or "") for record, actor in records]
        limit = getattr(q, "limit", None)
        return rows[:limit] if limit else rows

    def all(self) -> list[InboxRow]:
        with self._session() as session:
            records = session.execute(select(NotificationsInbox)).scalars().all()
        return [_as_row(r) for r in records]


_DEFAULT_STORE = None


def default_inbox_store():
    """The process-wide inbox store: Postgres when reachable, else in-memory.

    The same regression `preferences.py` already carries a note about, one
    table over. `SendContext` defaulted to `InMemoryInboxStore`
    unconditionally, so an alert written by `axi notifications send` in one
    process was invisible to a chat reading the inbox in another — and to the
    monitor daemon, and to every other reader. An always-in-memory inbox is a
    notification system that only works inside a single process.

    In-memory remains the fallback for unit tests and dev boxes with no
    database, where it is the right answer rather than a silent demotion.
    """
    global _DEFAULT_STORE
    if _DEFAULT_STORE is None:
        _DEFAULT_STORE = _try_build_database_store() or _in_memory()
    return _DEFAULT_STORE


def set_default_inbox_store(store) -> None:
    """Test seam — swap or reset the process-level default."""
    global _DEFAULT_STORE
    _DEFAULT_STORE = store


def _in_memory():
    from .inbox import InMemoryInboxStore

    return InMemoryInboxStore()


def _try_build_database_store():
    """Build the Postgres-backed store, or ``None`` when no DB is reachable."""
    try:
        from axiom.infra.db import engine_for

        engine, _schema = engine_for("notifications")
        # Bootstrap so a fresh install works before `axi db migrate` has run
        # the extension's history. Both tables: an inbox row exists because a
        # delivery receipt does, and the foreign key needs its target.
        from .db_models import DeliveryReceipt, NotificationsInbox

        DeliveryReceipt.__table__.create(bind=engine, checkfirst=True)
        NotificationsInbox.__table__.create(bind=engine, checkfirst=True)
        return DatabaseInboxStore()
    except Exception as exc:  # noqa: BLE001 — no database is a normal dev posture
        # Say so. Falling back to in-memory is right on a dev box and wrong in
        # a serving process, and the two are indistinguishable from the
        # outside: a notification system that works inside one process looks
        # exactly like one that works. The alert path fails loud for this
        # reason; the fallback should at least be audible.
        _LOG.warning(
            "notifications inbox falling back to IN-MEMORY (%s: %s) — alerts "
            "written here are invisible to every other process",
            type(exc).__name__,
            exc,
        )
        return None


__all__ = [
    "DatabaseInboxStore",
    "default_inbox_store",
    "set_default_inbox_store",
]
