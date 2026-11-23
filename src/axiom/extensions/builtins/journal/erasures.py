# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A reference loses the words of a record whose content was removed (unit LD-6).

The journal never holds a record, but a reference to a signed entry carries the entry's title as
its summary, and the attestations lane is kept for life. When the record's content is removed by
a signed tombstone, :func:`erase` writes one ``journal_erasures`` row per reference (append only,
naming the tombstone) and sets each row's summary to :data:`REMOVED`; the database guard allows
exactly that change (migration 0002). The reference itself (ids, digest, link) stays.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from . import store
from .db_models import JournalErasure, JournalEvent

#: What a reference says once the record's content is gone.
REMOVED = "Entry content removed under the log's retention rule"


def erase(source: str, source_ids: Iterable[str], *, reason: Mapping[str, Any]) -> int:
    """Remove the words of every reference from ``source`` to ``source_ids``. Returns how many
    rows changed. Idempotent: a reference already erased is left as it is."""
    ids = [str(i) for i in source_ids]
    if not ids:
        return 0
    now = datetime.now(UTC)
    with store.session_scope() as s:
        for sid in ids:
            s.execute(pg_insert(JournalErasure).values(source=source, source_id=sid, summary=REMOVED,
                                                       reason=dict(reason), at=now)
                      .on_conflict_do_nothing(index_elements=["source", "source_id"]))  # fmt: skip
        rows = list(s.execute(select(JournalEvent).where(
            JournalEvent.source == source, JournalEvent.source_id.in_(ids), JournalEvent.summary != REMOVED)).scalars())  # fmt: skip
        for row in rows:
            row.summary = REMOVED
        s.commit()
    return len(rows)


__all__ = ["REMOVED", "erase"]
