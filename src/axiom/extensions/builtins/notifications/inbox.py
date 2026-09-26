# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Inbox query + write API + in-memory store.

SEC-1 ships the in-memory store; HERALD-2 swaps in the Postgres-backed
store via ``axiom.infra.db.session_for("notifications")`` per ADR-052.

The in-memory store is NOT a stopgap — it remains the test fixture for
all downstream consumers (so tests don't need a live Postgres). It
satisfies the same ``InboxStore`` protocol the Postgres store will.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

from axiom.governance import Classification, classification_lte


@dataclass(frozen=True)
class InboxRow:
    """A row in ``notifications.notifications_inbox`` (spec §8)."""

    id: str
    receipt_id: str
    recipient: str
    classification: Classification
    priority: str
    summary: str
    read_at: datetime | None = None
    acknowledged_at: datetime | None = None
    acknowledged_by: str = ""
    muted: bool = False
    link: str = ""
    actor: str = ""
    """Who reported it. Read from the delivery receipt, not stored twice — the
    receipt is where provenance lives and duplicating it invites the two to
    disagree."""
    """Where to go for more, or to act. Persisted in the existing `body_ref`
    column — a reference to the fuller body is exactly what a link is, so this
    needs no new column.

    Emitted as a BARE URL rather than an OSC-8 hyperlink: the TUI renders
    through prompt_toolkit, which does not pass raw escape sequences through a
    Buffer. Terminals auto-link bare URLs, so cmd+click works anyway — and it
    still reads correctly where it does not."""
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        """Hold the row to its OWN declared types, at the one place every
        store passes through.

        `classification` is annotated `Classification`, but nothing enforced
        it, and both stores handed back whatever their caller happened to
        supply: the database one always a string (it round-trips a column),
        the in-memory one an enum only because its caller had already parsed
        one. So `row.classification.value` worked in tests and raised
        `AttributeError` against a real database — and the in-memory store's
        correctness was accidental, dependent on who called it.

        Normalizing here rather than in each store fixes the class rather than
        the instance: a third store cannot reintroduce it. An unrecognized
        label is left as-is instead of raising — a read must not be the thing
        that fails over a value already written.
        """
        if not isinstance(self.classification, Classification):
            try:
                object.__setattr__(
                    self,
                    "classification",
                    Classification.from_str(str(self.classification)),
                )
            except ValueError:
                pass


@dataclass(frozen=True)
class InboxQuery:
    """A query against the inbox.

    ``max_classification`` caps results at-or-below the supplied tier —
    matches the recipient-tier guard used by mobile + chat surfaces.
    """

    recipient: str
    unread_only: bool = False
    max_classification: Classification | None = None
    limit: int = 50


class InboxStore(Protocol):
    """Persistence contract — in-memory + Postgres implementations conform."""

    durable: bool
    """Whether a row written here outlives the process that wrote it.

    A caller whose purpose is to reach a person needs this: writing to a store
    that dies at exit is not a delivery, and it must not be reported as one.
    """

    def write(
        self,
        *,
        recipient: str,
        receipt_id: str,
        classification: Classification,
        priority: str,
        summary: str,
    ) -> str: ...

    def mark_read(self, *, row_id: str) -> None: ...

    def query(self, q: InboxQuery) -> list[InboxRow]: ...

    def all(self) -> list[InboxRow]: ...


class InMemoryInboxStore:
    """Test + SEC-1-default in-memory inbox store."""

    durable = False

    def __init__(self) -> None:
        self._rows: dict[str, InboxRow] = {}

    def write(
        self,
        *,
        recipient: str,
        receipt_id: str,
        classification: Classification,
        priority: str,
        summary: str,
    ) -> str:
        row_id = f"inbox-{uuid.uuid4().hex[:12]}"
        self._rows[row_id] = InboxRow(
            id=row_id,
            receipt_id=receipt_id,
            recipient=recipient,
            classification=classification,
            priority=priority,
            summary=summary,
        )
        return row_id

    def mark_read(self, *, row_id: str) -> None:
        if row_id not in self._rows:
            raise KeyError(row_id)
        row = self._rows[row_id]
        self._rows[row_id] = InboxRow(
            id=row.id,
            receipt_id=row.receipt_id,
            recipient=row.recipient,
            classification=row.classification,
            priority=row.priority,
            summary=row.summary,
            read_at=datetime.now(UTC),
            acknowledged_at=row.acknowledged_at,
            muted=row.muted,
            created_at=row.created_at,
        )

    def query(self, q: InboxQuery) -> list[InboxRow]:
        def admit(r: InboxRow) -> bool:
            if r.recipient != q.recipient:
                return False
            if q.unread_only and r.read_at is not None:
                return False
            if q.max_classification is not None and not classification_lte(
                r.classification, q.max_classification
            ):
                return False
            return True

        out = [r for r in self._rows.values() if admit(r)]
        out.sort(key=lambda r: r.created_at, reverse=True)
        return out[: q.limit]

    def all(self) -> list[InboxRow]:
        return list(self._rows.values())


def list_unread(store: InboxStore, *, recipient: str) -> list[InboxRow]:
    return store.query(InboxQuery(recipient=recipient, unread_only=True))


def mark_read(store: InboxStore, *, row_id: str) -> None:
    store.mark_read(row_id=row_id)


def _admit(r: InboxRow, q: InboxQuery) -> bool:
    """Exposed for tests; the logic is inside ``InMemoryInboxStore.query``."""
    if r.recipient != q.recipient:
        return False
    if q.unread_only and r.read_at is not None:
        return False
    if q.max_classification is not None and not classification_lte(
        r.classification, q.max_classification
    ):
        return False
    return True


def matches(rows: Iterable[InboxRow], q: InboxQuery) -> list[InboxRow]:
    return [r for r in rows if _admit(r, q)]


__all__ = [
    "InboxQuery",
    "InboxRow",
    "InboxStore",
    "InMemoryInboxStore",
    "list_unread",
    "mark_read",
    "matches",
]
