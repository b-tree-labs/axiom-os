# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Reply bindings that outlive the process (ADR-067 §3).

`ThreadStore` keeps bindings in memory, so a restart or a second gateway
process drops every outstanding ``[axi-corr: …]`` token and the human's reply
binds to nothing. `DbThreadStore` keeps them in the ``threads`` table behind
the same interface.

A binding is matched only inside the 24 h dedup window the token length is
sized for; older rows stay as a record but no longer bind. ``claim`` resolves a
token once, so a reply that arrives twice (a retry, two channels) acts once.
It returns a claim id, and ``release`` reopens the token for that id alone when
the action fails, so a reply that lost the claim cannot reopen the winner's.

ADR-052: storage goes through ``session_for("notifications")``; no engine is
built here and no schema is named.
"""

from __future__ import annotations

import logging
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session as _SASession

from axiom.governance.mode import is_dev

from ..db_models import Thread
from .threads import ThreadRecord, ThreadStore, body_token

_LOG = logging.getLogger(__name__)

#: The dedup window the 8-hex token is sized for (ADR-067 §3).
DEFAULT_WINDOW = timedelta(hours=24)


def _record(row: Thread) -> ThreadRecord:
    return ThreadRecord(
        correlation_id=row.correlation_id,
        token=row.token or body_token(row.correlation_id),
        actor=row.actor or "",
        vendor=row.channel,
        thread_ref=row.vendor_thread_id,
    )


class DbThreadStore:
    """Database-backed reply bindings. Same interface as :class:`ThreadStore`."""

    durable = True

    def __init__(self, engine=None, *, window: timedelta = DEFAULT_WINDOW) -> None:
        #: Injected only by tests; production goes through `session_for`.
        self._engine = engine
        self._window = window

    @contextmanager
    def _session(self):
        if self._engine is not None:
            with _SASession(self._engine) as session:
                yield session
            return
        from axiom.infra.db import session_for

        with session_for("notifications") as session:
            yield session

    def _find(self, session, *where) -> Thread | None:
        """The newest binding matching ``where`` inside the window."""
        cutoff = datetime.now(UTC) - self._window
        return session.scalars(
            select(Thread)
            .where(*where, Thread.created_at >= cutoff)
            .order_by(Thread.created_at.desc())
            .limit(1)
        ).first()

    def _open_row(self, session, token: str) -> Thread | None:
        return self._find(session, Thread.token == token)

    def bind(
        self,
        correlation_id: str,
        *,
        actor: str,
        vendor: str,
        thread_ref: str | None = None,
        receipt_id: str | None = None,
    ) -> ThreadRecord:
        """Store a binding. A ``receipt_id``, when given, must already be committed."""
        row = Thread(
            correlation_id=correlation_id,
            receipt_id=receipt_id,
            channel=vendor,
            vendor_thread_id=thread_ref,
            actor=actor,
            token=body_token(correlation_id),
            cross_channel=False,
            created_at=datetime.now(UTC),
        )
        with self._session() as session:
            session.add(row)
            session.commit()
            return _record(row)

    def by_token(self, token: str) -> ThreadRecord | None:
        with self._session() as session:
            row = self._open_row(session, token)
            return _record(row) if row is not None else None

    def by_correlation(self, correlation_id: str) -> ThreadRecord | None:
        with self._session() as session:
            row = self._find(session, Thread.correlation_id == correlation_id)
            return _record(row) if row is not None else None

    def claim(self, token: str) -> str | None:
        """Resolve ``token`` once. The row is claimed before anything acts, so of
        two replies carrying the same token only one gets a claim id, and the
        other gets ``None``. Only that id can reopen the token."""
        with self._session() as session:
            row = self._open_row(session, token)
            if row is None:
                return None
            claim_id = uuid.uuid4().hex
            changed = session.execute(
                update(Thread)
                .where(
                    Thread.correlation_id == row.correlation_id,
                    Thread.resolved_at.is_(None),
                )
                .values(resolved_at=datetime.now(UTC), claim_id=claim_id)
            ).rowcount
            session.commit()
            return claim_id if changed == 1 else None

    def release(self, token: str, claim_id: str | None) -> bool:
        """Reopen a token held by ``claim_id``, when the action it triggered
        failed. True if this reopened it. One statement, conditional on the
        claim, so a reply that lost the claim cannot reopen the winner's, and
        an old claim cannot reopen a newer one."""
        if claim_id is None:
            return False
        with self._session() as session:
            changed = session.execute(
                update(Thread)
                .where(
                    Thread.token == token,
                    Thread.claim_id == claim_id,
                    Thread.resolved_at.is_not(None),
                )
                .values(resolved_at=None, claim_id=None)
            ).rowcount
            session.commit()
            return changed == 1

    def schema_problem(self) -> str | None:
        """Why this store can't run here, or ``None`` when it can.

        Selects every mapped column through the session the store itself uses,
        so the answer covers the schema it will really look in, and a column a
        later migration adds is covered without changing this. It reads only:
        `migrate` owns the schema, and 0001's plain ``create_table`` needs to
        find it empty. The value is the exception's type, because a driver's
        message can carry the connection string.
        """
        try:
            with self._session() as session:
                session.execute(select(*Thread.__table__.columns).limit(0))
        except Exception as exc:  # noqa: BLE001 — any failure means "can't run here"
            return type(exc).__name__
        return None


_DEFAULT_STORE = None


def default_thread_store():
    """The process-wide binding store.

    The database store whenever the schema is migrated. When it isn't, or the
    database is down:

    - In dev, the in-memory store with a warning, which is right for unit tests
      and dev boxes.
    - Outside dev, still the database store, with an ERROR. Memory would look
      like it works until the first restart dropped every outstanding binding,
      which is the loss this store exists to prevent, and the caller could not
      tell. Each call raises instead, so the failure is visible at the moment
      a binding can't be kept, and the store works as soon as the migration
      lands or the database is back, with no restart.
    """
    global _DEFAULT_STORE
    if _DEFAULT_STORE is None:
        _DEFAULT_STORE = _build_default_store()
    return _DEFAULT_STORE


def set_default_thread_store(store) -> None:
    """Test seam — swap or reset the process-level default."""
    global _DEFAULT_STORE
    _DEFAULT_STORE = store


def _build_default_store() -> DbThreadStore | ThreadStore:
    store = DbThreadStore()
    problem = store.schema_problem()
    if problem is None:
        return store
    if is_dev():
        _LOG.warning(
            "reply bindings falling back to IN-MEMORY (%s) — a restart drops "
            "every outstanding [axi-corr] token",
            problem,
        )
        return ThreadStore()
    _LOG.error(
        "notifications threads table is not usable (%s): the database is down or "
        "its migrations have not run. Binding a reply raises until they have; "
        "no restart is needed afterwards",
        problem,
    )
    return store


__all__ = [
    "DEFAULT_WINDOW",
    "DbThreadStore",
    "default_thread_store",
    "set_default_thread_store",
]
