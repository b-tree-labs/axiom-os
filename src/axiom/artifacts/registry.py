# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""ArtifactRegistry — content-addressed, versioned, optionally signed.

Pluggable backend:
- InMemoryBackend (default): ephemeral dict, legacy behavior.
- SQLiteBackend: zero-config file-based persistence (standalone-first).
- Future: PostgresBackend for multi-process / federated deployments.

Version chain: multiple artifacts with the same (kind, name) form a
chain. `latest()` returns the most recent non-deleted entry.
`version_chain()` returns the full list, earliest first — useful for
audit + rollback.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

Signer = Callable[[bytes], bytes]


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class Artifact:
    id: str
    kind: str
    name: str
    data: dict[str, Any]
    content_hash: str
    created_at: float
    signature: bytes | None = None
    deleted: bool = False
    deletion_reason: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Backend protocol
# ---------------------------------------------------------------------------


class ArtifactBackend(Protocol):
    def put(self, artifact: Artifact) -> None: ...
    def get(self, artifact_id: str) -> Artifact | None: ...
    def list_all(
        self, kind: str | None = None, include_deleted: bool = False
    ) -> list[Artifact]: ...
    def mark_deleted(self, artifact_id: str, reason: str | None) -> None: ...


# ---------------------------------------------------------------------------
# InMemoryBackend (default)
# ---------------------------------------------------------------------------


class InMemoryBackend:
    def __init__(self) -> None:
        self._store: dict[str, Artifact] = {}

    def put(self, artifact: Artifact) -> None:
        self._store[artifact.id] = artifact

    def get(self, artifact_id: str) -> Artifact | None:
        return self._store.get(artifact_id)

    def list_all(
        self, kind: str | None = None, include_deleted: bool = False
    ) -> list[Artifact]:
        out = []
        for a in self._store.values():
            if kind is not None and a.kind != kind:
                continue
            if a.deleted and not include_deleted:
                continue
            out.append(a)
        return out

    def mark_deleted(self, artifact_id: str, reason: str | None) -> None:
        a = self._store.get(artifact_id)
        if a is None:
            raise KeyError(artifact_id)
        a.deleted = True
        a.deletion_reason = reason


# ---------------------------------------------------------------------------
# SQLiteBackend
# ---------------------------------------------------------------------------


class SQLiteBackend:
    """File-based persistence. Standalone-first default.

    Connections are thread-local so the backend can be safely shared
    across the FastAPI/uvicorn worker pool without ``sqlite3.ProgrammingError:
    SQLite objects created in a thread can only be used in that same
    thread``. SQLite handles file-level locking for concurrent writes
    via ``threading.Lock`` is unnecessary because each thread has its
    own connection.
    """

    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._tls = threading.local()
        # Eagerly initialize the schema on the construction thread so a
        # connect-then-die pattern doesn't leave the file empty.
        self._init_schema()

    @property
    def _conn(self) -> sqlite3.Connection:
        """Thread-local connection. Lazy-creates on first access per thread."""
        conn = getattr(self._tls, "conn", None)
        if conn is None:
            conn = sqlite3.connect(str(self._path))
            self._tls.conn = conn
        return conn

    def _init_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS artifacts (
                id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                name TEXT NOT NULL,
                data_json TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                created_at REAL NOT NULL,
                signature BLOB,
                deleted INTEGER NOT NULL DEFAULT 0,
                deletion_reason TEXT,
                metadata_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE INDEX IF NOT EXISTS idx_artifacts_kind_name
                ON artifacts(kind, name);
            CREATE INDEX IF NOT EXISTS idx_artifacts_created_at
                ON artifacts(created_at);

            -- Stage 3 expression indexes for memory-fragment projections
            -- (ADR-033 + spec-memory §6). Targets the two domain-agnostic
            -- filter dimensions every projection cares about: the MIRIX
            -- cognitive type + the originating principal. The third
            -- common dimension (scope) lives in the per-extension content
            -- payload — no top-level field on MemoryFragment yet — so
            -- scope filtering is post-hoc in SQL after this index winnows.
            -- Promote to a covering index when MemoryFragment gains a
            -- top-level scope field (open question in spec-memory §14).
            CREATE INDEX IF NOT EXISTS idx_artifacts_fragment_principal
                ON artifacts(
                    kind,
                    json_extract(data_json, '$.cognitive_type'),
                    json_extract(data_json, '$.provenance.principal_id')
                ) WHERE kind = 'fragment' AND deleted = 0;
            """
        )
        self._conn.commit()

    def put(self, artifact: Artifact) -> None:
        self._conn.execute(
            """
            INSERT OR REPLACE INTO artifacts
                (id, kind, name, data_json, content_hash, created_at,
                 signature, deleted, deletion_reason, metadata_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                artifact.id,
                artifact.kind,
                artifact.name,
                json.dumps(artifact.data, sort_keys=True),
                artifact.content_hash,
                artifact.created_at,
                artifact.signature,
                1 if artifact.deleted else 0,
                artifact.deletion_reason,
                json.dumps(artifact.metadata or {}, sort_keys=True),
            ),
        )
        self._conn.commit()

    def _row_to_artifact(self, row) -> Artifact:
        return Artifact(
            id=row[0],
            kind=row[1],
            name=row[2],
            data=json.loads(row[3]),
            content_hash=row[4],
            created_at=row[5],
            signature=row[6],
            deleted=bool(row[7]),
            deletion_reason=row[8],
            metadata=json.loads(row[9] or "{}"),
        )

    def get(self, artifact_id: str) -> Artifact | None:
        cur = self._conn.execute(
            "SELECT id, kind, name, data_json, content_hash, created_at, "
            "signature, deleted, deletion_reason, metadata_json "
            "FROM artifacts WHERE id = ?",
            (artifact_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return self._row_to_artifact(row)

    def list_all(
        self, kind: str | None = None, include_deleted: bool = False
    ) -> list[Artifact]:
        clauses = []
        params: list[Any] = []
        if kind is not None:
            clauses.append("kind = ?")
            params.append(kind)
        if not include_deleted:
            clauses.append("deleted = 0")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        cur = self._conn.execute(
            f"SELECT id, kind, name, data_json, content_hash, created_at, "
            f"signature, deleted, deletion_reason, metadata_json "
            f"FROM artifacts {where} ORDER BY created_at ASC",
            params,
        )
        return [self._row_to_artifact(r) for r in cur.fetchall()]

    # ----- Stage 3 fast path for projections (ADR-033, see spec-memory §6) ---
    # Projections that need to filter fragments by JSON content (cognitive
    # type, scope, principal) used to iterate the full table and decode
    # every JSON blob in Python — O(N) per projection call. SQLite's JSON1
    # extension lets us push the filter into the engine: O(K) where K is
    # the matching subset.
    #
    # The method shape is deliberately specific to memory-fragment use
    # cases (cognitive_type + principal_id + scope_path/value) rather than
    # a general-purpose JSON query — tighter contract = faster index plan
    # = clearer call sites.

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
        """SQLite-side filtered listing for memory-fragment projections.

        Pushes JSON1 predicates into the SELECT so projections don't
        decode every fragment blob in Python. ``scope_path`` is the
        JSON path inside ``data.content`` to match against ``scope_value``
        (e.g., ``classroom_id``); when both are supplied, the SQL filter
        adds ``AND json_extract(data_json, '$.content.<path>') = ?``.
        """
        clauses = ["kind = 'fragment'"]
        params: list[Any] = []
        if not include_deleted:
            clauses.append("deleted = 0")
        if cognitive_type is not None:
            clauses.append("json_extract(data_json, '$.cognitive_type') = ?")
            params.append(cognitive_type)
        if principal_id is not None:
            clauses.append(
                "json_extract(data_json, '$.provenance.principal_id') = ?"
            )
            params.append(principal_id)
        if scope_path is not None and scope_value is not None:
            clauses.append(
                f"json_extract(data_json, '$.content.{scope_path}') = ?"
            )
            params.append(scope_value)

        order = ""
        if order_by_event_time_desc:
            # event_time lives in content per the EPISODIC validator; fall
            # back to created_at when content has no event_time field.
            order = (
                " ORDER BY COALESCE("
                "json_extract(data_json, '$.content.event_time'),"
                " created_at) DESC"
            )

        limit_clause = f" LIMIT {int(limit)}" if limit else ""
        sql = (
            f"SELECT id, kind, name, data_json, content_hash, created_at, "
            f"signature, deleted, deletion_reason, metadata_json "
            f"FROM artifacts WHERE {' AND '.join(clauses)}{order}{limit_clause}"
        )
        cur = self._conn.execute(sql, params)
        return [self._row_to_artifact(r) for r in cur.fetchall()]

    def mark_deleted(self, artifact_id: str, reason: str | None) -> None:
        self._conn.execute(
            "UPDATE artifacts SET deleted = 1, deletion_reason = ? WHERE id = ?",
            (reason, artifact_id),
        )
        self._conn.commit()


# ---------------------------------------------------------------------------
# ArtifactRegistry
# ---------------------------------------------------------------------------


class ArtifactRegistry:
    """Content-addressed registry with pluggable backend + version chains."""

    def __init__(
        self,
        backend: ArtifactBackend | None = None,
        *,
        signer: Signer | None = None,
    ) -> None:
        self._backend = backend if backend is not None else InMemoryBackend()
        self._signer = signer

    def register(
        self,
        *,
        kind: str,
        name: str,
        data: dict[str, Any],
        metadata: dict[str, Any] | None = None,
    ) -> str:
        payload = {"kind": kind, "name": name, "data": data}
        canonical = json.dumps(payload, sort_keys=True).encode("utf-8")
        content_hash = hashlib.sha256(canonical).hexdigest()

        artifact = Artifact(
            id=uuid.uuid4().hex,
            kind=kind,
            name=name,
            data=data,
            content_hash=content_hash,
            created_at=time.time(),
            signature=self._signer(canonical) if self._signer else None,
            metadata=metadata or {},
        )
        self._backend.put(artifact)
        return artifact.id

    def get(self, artifact_id: str) -> Artifact:
        a = self._backend.get(artifact_id)
        if a is None:
            raise KeyError(artifact_id)
        return a

    def list(
        self, *, kind: str | None = None, include_deleted: bool = False
    ) -> list[Artifact]:
        return self._backend.list_all(kind=kind, include_deleted=include_deleted)

    def delete(self, artifact_id: str, *, reason: str | None = None) -> None:
        self._backend.mark_deleted(artifact_id, reason)

    # --- Version chain ------------------------------------------------------

    def version_chain(self, *, kind: str, name: str) -> list[Artifact]:
        """Return all artifacts for (kind, name), earliest first. Excludes
        tombstones by default — use `list(include_deleted=True)` for that."""
        all_kind = self._backend.list_all(kind=kind, include_deleted=True)
        return sorted(
            [a for a in all_kind if a.name == name],
            key=lambda a: a.created_at,
        )

    def latest(self, *, kind: str, name: str) -> Artifact | None:
        """Most recent non-deleted artifact for (kind, name), or None."""
        chain = self.version_chain(kind=kind, name=name)
        for a in reversed(chain):
            if not a.deleted:
                return a
        return None
