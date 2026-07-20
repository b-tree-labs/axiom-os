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
    muted: bool = False
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


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
