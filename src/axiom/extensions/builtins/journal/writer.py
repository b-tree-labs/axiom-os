# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The one append. Idempotent on ``(source, source_id)``; creates the month's partition first.

Live ingest and every catch-up reader call :func:`append`. The key is inserted first with
``ON CONFLICT DO NOTHING``; only a key that landed is followed by the event row, in the same
transaction, so a replay, a live event and a catch-up for one record make one row. Listeners
(the live stream) are told after the commit.
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
import zlib
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text

from . import store
from .rows import Row

log = logging.getLogger("axiom.journal.writer")

_listeners: list[Callable[[dict[str, Any]], None]] = []
_partitions: set[str] = set()
_lock = threading.Lock()

ROW_COLUMNS = (
    "seq", "id", "at", "recorded_at", "lane", "subject", "source", "source_id", "status",
    "actor", "summary", "subjects", "ref", "link", "site", "logbook", "classification",
    "retention_class",
)  # fmt: skip


def on_append(fn: Callable[[dict[str, Any]], None]) -> Callable[[], None]:
    """Be told of every row this process appends; returns the unsubscribe."""
    with _lock:
        _listeners.append(fn)

    def off() -> None:
        with _lock:
            if fn in _listeners:
                _listeners.remove(fn)

    return off


def _month_bounds(at: datetime) -> tuple[datetime, datetime]:
    lo = at.astimezone(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    hi = lo.replace(year=lo.year + 1, month=1) if lo.month == 12 else lo.replace(month=lo.month + 1)
    return lo, hi


def ensure_partition(session: Any, at: datetime) -> str:
    """Create ``journal_events_p<YYYYMM>`` for ``at``'s month if this process has not yet.

    ``CREATE TABLE IF NOT EXISTS`` under an advisory lock, so two writers in two processes
    cannot both create it. The name resolves in the session's ``search_path``, which
    ``session_for`` sets to the journal schema.
    """
    lo, hi = _month_bounds(at)
    name = f"journal_events_p{lo:%Y%m}"
    if name in _partitions:
        return name
    key = zlib.crc32(name.encode()) & 0x7FFFFFFF
    session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})
    ddl = (
        f"CREATE TABLE IF NOT EXISTS {name} PARTITION OF journal_events "
        f"FOR VALUES FROM ('{lo.isoformat()}') TO ('{hi.isoformat()}')"
    )
    # nosemgrep: avoid-sqlalchemy-text -- `name` is journal_events_p<YYYYMM> from a datetime and the bounds are that month's ISO dates, nothing a caller supplied; an identifier cannot be bound
    session.execute(text(ddl))
    _partitions.add(name)
    return name


def forget_partitions() -> None:
    """Test isolation: the next append checks the partition again."""
    _partitions.clear()


def row_json(m: Any) -> dict[str, Any]:
    """A stored row as surfaces return it."""
    out: dict[str, Any] = {}
    for col in ROW_COLUMNS:
        v = m[col]
        if isinstance(v, datetime):
            v = (v if v.tzinfo else v.replace(tzinfo=UTC)).isoformat()
        elif col == "id":
            v = str(v)
        out[col] = v
    return out


def append(row: Row) -> dict[str, Any] | None:
    """Append one row; the stored row, or None when ``(source, source_id)`` was already there."""
    with store.session_scope() as s:
        ensure_partition(s, row.at)
        landed = s.execute(
            text(
                "INSERT INTO journal_keys (source, source_id, at) VALUES (:source, :source_id, :at) "
                "ON CONFLICT (source, source_id) DO NOTHING RETURNING source"
            ),
            {"source": row.source, "source_id": row.source_id, "at": row.at},
        ).scalar()
        if landed is None:
            s.rollback()
            return None
        stored = (
            s.execute(
                text(
                    "INSERT INTO journal_events (id, at, recorded_at, lane, subject, source, source_id, "
                    "status, actor, summary, subjects, ref, link, site, logbook, classification, "
                    "retention_class) VALUES (:id, :at, :recorded_at, :lane, :subject, :source, "
                    ":source_id, :status, :actor, :summary, CAST(:subjects AS jsonb), CAST(:ref AS jsonb), "
                    ":link, :site, :logbook, :classification, :retention_class) "
                    "RETURNING *"
                ),
                {
                    "id": str(uuid.uuid4()),
                    "at": row.at,
                    "recorded_at": datetime.now(UTC),
                    "lane": row.lane,
                    "subject": row.subject,
                    "source": row.source,
                    "source_id": row.source_id,
                    "status": row.status,
                    "actor": row.actor,
                    "summary": row.summary,
                    "subjects": json.dumps(list(row.subjects)),
                    "ref": json.dumps(row.ref, default=str),
                    "link": row.link,
                    "site": row.site,
                    "logbook": row.logbook,
                    "classification": row.classification,
                    "retention_class": row.retention_class,
                },
            )
            .mappings()
            .one()
        )
        out = row_json(stored)
        s.commit()
    with _lock:
        listeners = list(_listeners)
    for fn in listeners:
        try:
            fn(out)
        except Exception:  # noqa: BLE001 - a listener never fails the append
            log.debug("journal listener failed", exc_info=True)
    return out


__all__ = [
    "ROW_COLUMNS",
    "append",
    "ensure_partition",
    "forget_partitions",
    "on_append",
    "row_json",
]
