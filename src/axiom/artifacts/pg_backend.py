# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Postgres ledger backend for ``ArtifactRegistry`` (ADR-174).

Implements the same ``ArtifactBackend`` protocol as the in-memory and SQLite
backends, plus ``find_fragments`` (the memory projections' fast path), against
the ``memory`` schema. A *scope* names one ledger: the user's own memory, a
classroom, an extension's runtime scope. Scopes share tables and never mix rows.
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from axiom.artifacts.registry import Artifact
from axiom.memory.pg_models import ArtifactRow
from axiom.memory.pg_store import session_scope


def _to_artifact(row: ArtifactRow) -> Artifact:
    return Artifact(
        id=row.id,
        kind=row.kind,
        name=row.name,
        data=row.data,
        content_hash=row.content_hash,
        created_at=float(row.created_at),
        signature=bytes(row.signature) if row.signature is not None else None,
        deleted=bool(row.deleted),
        deletion_reason=row.deletion_reason,
        metadata=row.meta or {},
    )


def _event_time(a: Artifact) -> float:
    """The fragment's event time as epoch seconds, else when it was recorded."""
    raw = ((a.data or {}).get("content") or {}).get("event_time")
    if isinstance(raw, (int, float)):
        return float(raw)
    if isinstance(raw, str):
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    return a.created_at


class PostgresBackend:
    """One ledger scope in Postgres."""

    def __init__(self, scope: str) -> None:
        if not scope:
            raise ValueError("a ledger scope is required")
        self.scope = scope

    # ----- writes -----

    def put(self, artifact: Artifact) -> None:
        values = dict(
            kind=artifact.kind,
            name=artifact.name,
            data=artifact.data,
            content_hash=artifact.content_hash,
            created_at=artifact.created_at,
            signature=artifact.signature,
            deleted=bool(artifact.deleted),
            deletion_reason=artifact.deletion_reason,
            meta=artifact.metadata or {},
        )
        for attempt in (1, 2):
            with session_scope() as s:
                row = s.get(ArtifactRow, (self.scope, artifact.id))
                if row is not None:
                    for k, v in values.items():
                        setattr(row, k, v)
                else:
                    s.add(
                        ArtifactRow(scope=self.scope, id=artifact.id, seq=time.time_ns(), **values)
                    )
                try:
                    s.commit()
                    return
                except IntegrityError:
                    # A concurrent writer inserted the same id first; retry as an update.
                    s.rollback()
                    if attempt == 2:
                        raise

    def mark_deleted(self, artifact_id: str, reason: str | None) -> None:
        with session_scope() as s:
            result = s.execute(
                update(ArtifactRow)
                .where(ArtifactRow.scope == self.scope, ArtifactRow.id == artifact_id)
                .values(deleted=True, deletion_reason=reason)
            )
            s.commit()
            if result.rowcount == 0:
                raise KeyError(artifact_id)

    # ----- reads -----

    def get(self, artifact_id: str) -> Artifact | None:
        with session_scope() as s:
            row = s.get(ArtifactRow, (self.scope, artifact_id))
            return _to_artifact(row) if row is not None else None

    def _select(self, *clauses: Any):
        return (
            select(ArtifactRow)
            .where(ArtifactRow.scope == self.scope, *clauses)
            .order_by(ArtifactRow.created_at.asc(), ArtifactRow.seq.asc())
        )

    def list_all(self, kind: str | None = None, include_deleted: bool = False) -> list[Artifact]:
        clauses: list[Any] = []
        if kind is not None:
            clauses.append(ArtifactRow.kind == kind)
        if not include_deleted:
            clauses.append(ArtifactRow.deleted.is_(False))
        with session_scope() as s:
            return [_to_artifact(r) for r in s.execute(self._select(*clauses)).scalars()]

    def find_by_name(self, kind: str, name: str, include_deleted: bool = False) -> list[Artifact]:
        clauses: list[Any] = [ArtifactRow.kind == kind, ArtifactRow.name == name]
        if not include_deleted:
            clauses.append(ArtifactRow.deleted.is_(False))
        with session_scope() as s:
            return [_to_artifact(r) for r in s.execute(self._select(*clauses)).scalars()]

    def find_fragments(
        self,
        *,
        cognitive_type: str | None = None,
        principal_id: str | None = None,
        scope_path: str | None = None,
        scope_value: str | None = None,
        order_by_event_time_desc: bool = False,
        limit: int | None = None,
        include_deleted: bool = False,
    ) -> list[Artifact]:
        """Filtered listing for memory-fragment projections.

        The filters run in the database against the JSON document. ``scope_path``
        is a dotted path inside ``data.content`` and is bound as a parameter, never
        interpolated into SQL. Event-time ordering is applied after the filtered
        fetch, in Python, because the stored value may be an ISO string or a
        number and the old SQL compared the two kinds, which does not order.
        """
        clauses: list[Any] = [ArtifactRow.kind == "fragment"]
        if not include_deleted:
            clauses.append(ArtifactRow.deleted.is_(False))
        if cognitive_type is not None:
            clauses.append(ArtifactRow.data["cognitive_type"].as_string() == cognitive_type)
        if principal_id is not None:
            clauses.append(
                ArtifactRow.data[("provenance", "principal_id")].as_string() == principal_id
            )
        if scope_path is not None and scope_value is not None:
            parts = ("content", *scope_path.split("."))
            clauses.append(ArtifactRow.data[parts].as_string() == scope_value)
        stmt = self._select(*clauses)
        if not order_by_event_time_desc and limit:
            stmt = stmt.limit(int(limit))
        with session_scope() as s:
            found = [_to_artifact(r) for r in s.execute(stmt).scalars()]
        if order_by_event_time_desc:
            found.sort(key=_event_time, reverse=True)
            if limit:
                found = found[: int(limit)]
        return found


def open_backend(scope: str) -> PostgresBackend:
    """The production ledger for ``scope``. Raises MemoryStoreUnavailable with advice
    if Postgres cannot be used, instead of failing on the first read."""
    from axiom.memory.pg_store import ensure_provisioned

    ensure_provisioned()
    return PostgresBackend(scope)
