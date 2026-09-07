# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""PostgreSQL-backed state store for domain-consumer agents.

Drop-in alternative to LockedJsonFile that provides:
- ACID transactions (real isolation, not advisory locks)
- Multi-process AND multi-machine safety
- Conflict detection via row-level locking
- Built-in audit trail
- Crash recovery via WAL

Same read/write interface as LockedJsonFile for transparent backend swapping.

Requires: psycopg[binary] (PostgreSQL adapter)

Usage::

    # Configure via environment or config
    store = PgStateStore("postgresql://localhost/axiom")

    # Same patterns as LockedJsonFile
    with store.open("runtime/inbox/state/briefing_state.json", exclusive=True) as state:
        data = state.read()
        data["counter"] += 1
        state.write(data)
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

# Schema version for forward compatibility
SCHEMA_VERSION = 1

_INIT_SQL = """\
CREATE TABLE IF NOT EXISTS agent_state (
    path         TEXT PRIMARY KEY,
    data         JSONB NOT NULL DEFAULT '{}',
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_by   TEXT NOT NULL DEFAULT '',
    version      INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS state_audit_log (
    id           BIGSERIAL PRIMARY KEY,
    path         TEXT NOT NULL,
    action       TEXT NOT NULL,
    old_version  INTEGER,
    new_version  INTEGER,
    actor        TEXT NOT NULL DEFAULT '',
    timestamp    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    details      JSONB
);

CREATE INDEX IF NOT EXISTS idx_state_audit_path ON state_audit_log(path);
CREATE INDEX IF NOT EXISTS idx_state_audit_time ON state_audit_log(timestamp);
"""


@dataclass
class PgStateHandle:
    """Handle to a single state document within a transaction."""

    path: str
    _conn: Any  # psycopg connection
    _exclusive: bool
    _initial_version: int | None = None

    def read(self) -> Any:
        """Read the JSON state. Returns empty dict if not found."""
        cur = self._conn.cursor()
        if self._exclusive:
            # SELECT ... FOR UPDATE — row-level exclusive lock
            cur.execute(
                "SELECT data, version FROM agent_state WHERE path = %s FOR UPDATE",
                (self.path,),
            )
        else:
            # SELECT ... FOR SHARE — row-level shared lock
            cur.execute(
                "SELECT data, version FROM agent_state WHERE path = %s FOR SHARE",
                (self.path,),
            )
        row = cur.fetchone()
        if row is None:
            self._initial_version = None
            return {}
        self._initial_version = row[1]
        return row[0]

    def write(self, data: Any) -> None:
        """Write JSON state with conflict detection."""
        if not self._exclusive:
            raise RuntimeError("write() requires exclusive=True")

        cur = self._conn.cursor()
        now = datetime.now(UTC)
        actor = os.environ.get("USER", "unknown")

        if self._initial_version is None:
            # INSERT new row
            cur.execute(
                """INSERT INTO agent_state (path, data, updated_at, updated_by, version)
                   VALUES (%s, %s, %s, %s, 1)
                   ON CONFLICT (path) DO UPDATE
                   SET data = EXCLUDED.data,
                       updated_at = EXCLUDED.updated_at,
                       updated_by = EXCLUDED.updated_by,
                       version = agent_state.version + 1""",
                (self.path, json.dumps(data), now, actor),
            )
            new_version = 1
        else:
            # UPDATE with optimistic concurrency check
            new_version = self._initial_version + 1
            cur.execute(
                """UPDATE agent_state
                   SET data = %s, updated_at = %s, updated_by = %s, version = %s
                   WHERE path = %s AND version = %s""",
                (json.dumps(data), now, actor, new_version,
                 self.path, self._initial_version),
            )
            if cur.rowcount == 0:
                raise ConcurrentModificationError(
                    f"State at '{self.path}' was modified by another process "
                    f"(expected version {self._initial_version})"
                )

        # Audit log
        cur.execute(
            """INSERT INTO state_audit_log (path, action, old_version, new_version, actor, details)
               VALUES (%s, %s, %s, %s, %s, %s)""",
            (self.path, "write", self._initial_version, new_version, actor,
             json.dumps({"size_bytes": len(json.dumps(data))})),
        )


class ConcurrentModificationError(Exception):
    """Raised when optimistic concurrency check fails."""


class PgStateStore:
    """PostgreSQL-backed state store with transaction-per-operation semantics."""

    def __init__(self, dsn: str | None = None):
        """Initialize with PostgreSQL connection string.

        Args:
            dsn: PostgreSQL connection string. If None, reads from
                 NEUTRON_STATE_DSN or DATABASE_URL environment variables.
        """
        self._dsn = dsn or os.environ.get(
            "NEUTRON_STATE_DSN",
            os.environ.get("DATABASE_URL", ""),
        )
        self._initialized = False

    def _get_connection(self):
        """Get a new connection (no pooling for simplicity)."""
        try:
            import psycopg
        except ImportError:
            raise ImportError(
                "psycopg not installed. Install with: pip install 'psycopg[binary]'"
            )
        return psycopg.connect(self._dsn)

    def ensure_schema(self) -> None:
        """Create tables if they don't exist."""
        if self._initialized:
            return
        conn = self._get_connection()
        try:
            conn.execute(_INIT_SQL)
            conn.commit()
            self._initialized = True
        finally:
            conn.close()

    @contextmanager
    def open(
        self,
        path: str,
        *,
        exclusive: bool = False,
    ) -> Generator[PgStateHandle, None, None]:
        """Open a state document within a transaction.

        Same interface as LockedJsonFile context manager.

        Usage::

            with store.open("my/state.json", exclusive=True) as state:
                data = state.read()
                data["key"] = "value"
                state.write(data)
        """
        self.ensure_schema()
        conn = self._get_connection()
        try:
            handle = PgStateHandle(
                path=path,
                _conn=conn,
                _exclusive=exclusive,
            )
            yield handle
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def read(self, path: str) -> Any:
        """Convenience: read a state document."""
        with self.open(path) as handle:
            return handle.read()

    def write(self, path: str, data: Any) -> None:
        """Convenience: write a state document."""
        with self.open(path, exclusive=True) as handle:
            handle.read()  # Load version for conflict detection
            handle.write(data)

    def delete(self, path: str) -> bool:
        """Delete a state document. Returns True if it existed."""
        self.ensure_schema()
        conn = self._get_connection()
        try:
            cur = conn.cursor()
            cur.execute("DELETE FROM agent_state WHERE path = %s", (path,))
            deleted = cur.rowcount > 0
            if deleted:
                cur.execute(
                    """INSERT INTO state_audit_log (path, action, actor)
                       VALUES (%s, 'delete', %s)""",
                    (path, os.environ.get("USER", "unknown")),
                )
            conn.commit()
            return deleted
        finally:
            conn.close()

    def list_paths(self, prefix: str = "") -> list[str]:
        """List all state document paths, optionally filtered by prefix."""
        self.ensure_schema()
        conn = self._get_connection()
        try:
            cur = conn.cursor()
            if prefix:
                cur.execute(
                    "SELECT path FROM agent_state WHERE path LIKE %s ORDER BY path",
                    (prefix + "%",),
                )
            else:
                cur.execute("SELECT path FROM agent_state ORDER BY path")
            return [row[0] for row in cur.fetchall()]
        finally:
            conn.close()

    def audit_log(
        self,
        path: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Query audit log entries."""
        self.ensure_schema()
        conn = self._get_connection()
        try:
            cur = conn.cursor()
            if path:
                cur.execute(
                    """SELECT path, action, old_version, new_version, actor, timestamp, details
                       FROM state_audit_log WHERE path = %s
                       ORDER BY timestamp DESC LIMIT %s""",
                    (path, limit),
                )
            else:
                cur.execute(
                    """SELECT path, action, old_version, new_version, actor, timestamp, details
                       FROM state_audit_log
                       ORDER BY timestamp DESC LIMIT %s""",
                    (limit,),
                )
            return [
                {
                    "path": row[0],
                    "action": row[1],
                    "old_version": row[2],
                    "new_version": row[3],
                    "actor": row[4],
                    "timestamp": row[5].isoformat() if row[5] else None,
                    "details": row[6],
                }
                for row in cur.fetchall()
            ]
        finally:
            conn.close()
