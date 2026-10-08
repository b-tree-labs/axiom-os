# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""RAGStore.connect bounds the schema-migration lock wait.

Regression for 2026-06-08: an ALTER TABLE in the schema migration took
AccessExclusiveLock, blocked on a concurrent reader with no lock_timeout,
and stalled the lock queue — every subsequent RAG query hung. connect()
must set a bounded lock_timeout *before* running the schema DDL.
"""

from __future__ import annotations

from unittest import mock

from axiom.rag.store import _SCHEMA_SQL, RAGStore


def test_connect_sets_lock_timeout_before_schema():
    executed: list[str] = []

    cur = mock.MagicMock()
    cur.execute.side_effect = lambda sql, *a, **k: executed.append(sql)
    cur.__enter__ = lambda s: s
    cur.__exit__ = lambda *a: False

    conn = mock.MagicMock()
    conn.closed = False
    conn.cursor.return_value = cur

    with mock.patch("axiom.rag.store.psycopg2.connect", return_value=conn):
        store = RAGStore("postgresql://x/y")
        # generation/interaction/audit sub-inits may no-op; we only assert ordering
        try:
            store.connect()
        except Exception:
            pass

    # lock_timeout SET must precede the schema DDL
    lock_idx = next(i for i, s in enumerate(executed) if "lock_timeout" in s.lower())
    schema_idx = next(i for i, s in enumerate(executed) if s == _SCHEMA_SQL)
    assert lock_idx < schema_idx, f"lock_timeout must precede schema DDL: {executed[:4]}"
