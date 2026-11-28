# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Opening the retrieval store to answer a chat turn must not need CREATE.

Observed on a hosted node: every web chat turn logged ``RAG store unavailable
(InsufficientPrivilege: permission denied for schema public ... CREATE TABLE IF
NOT EXISTS documents``) and answered with no retrieval context. The chat opened
the store with schema-ensure on, so every connect ran the migration DDL, and
Postgres checks CREATE on the schema before it checks whether the table exists:
``IF NOT EXISTS`` does not help a role that may only read. The serving role is
right not to hold CREATE on ``public`` (ADR-052); the read path was wrong to ask
for it. Ensuring the schema is install and index time work.

These tests drive the real ``ChatAgent._get_rag_store`` and the real
``RAGStore`` against a connection double that behaves like that role: any DDL
raises ``permission denied for schema public``.
"""

from __future__ import annotations

import re
from types import SimpleNamespace

import pytest

from axiom.extensions.builtins.chat.agent import ChatAgent
from axiom.rag import store as store_mod
from axiom.rag.store import RAGStore

DSN = "postgresql://reader@db.invalid/corpus"

_DDL = re.compile(r"^\s*(CREATE|ALTER|DROP|COMMENT|GRANT)\b", re.IGNORECASE | re.MULTILINE)


class _Denied(Exception):
    """Stands in for psycopg2's InsufficientPrivilege."""


class _Cursor:
    def __init__(self, log, deny_ddl):
        self._log = log
        self._deny_ddl = deny_ddl

    def execute(self, sql, params=None):
        self._log.append(sql)
        if self._deny_ddl and _DDL.search(sql):
            raise _Denied("permission denied for schema public")

    def fetchone(self):
        return None

    def fetchall(self):
        return []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Conn:
    def __init__(self, log, deny_ddl):
        self.closed = False
        self.autocommit = False
        self._log = log
        self._deny_ddl = deny_ddl

    def cursor(self, *a, **k):
        return _Cursor(self._log, self._deny_ddl)

    def close(self):
        self.closed = True


@pytest.fixture()
def pg(monkeypatch):
    """A fake Postgres; ``state['deny_ddl']`` makes it a read-only role."""
    log: list[str] = []
    state = {"deny_ddl": True}
    monkeypatch.setattr(store_mod.psycopg2, "connect", lambda dsn: _Conn(log, state["deny_ddl"]))
    from axiom.rag import store_factory

    monkeypatch.setattr(
        store_factory,
        "resolve_store_url",
        lambda **kw: store_factory.StoreUrl(DSN, "test"),
    )
    return log, state


def _open_for_chat():
    agent = SimpleNamespace(_rag_init_attempted=False, _rag_store=None)
    return ChatAgent._get_rag_store(agent)


def test_chat_opens_the_store_without_ddl_as_a_read_only_role(pg):
    log, _ = pg
    store = _open_for_chat()
    assert store is not None, "a role that may only read must still get the store"
    ddl = [s for s in log if _DDL.search(s)]
    assert ddl == [], f"serving-time open issued DDL: {ddl}"


def _tables(pattern: str, statements: list[str]) -> set[str]:
    out: set[str] = set()
    for s in statements:
        out.update(m.group(1) for m in re.finditer(pattern, s, re.IGNORECASE))
    return out


def test_chat_reads_the_tables_the_indexer_writes(pg):
    """Both resolve unqualified names through the role's search_path on the same
    DSN, so the reader finds the corpus the indexer built rather than an empty
    copy somewhere else. Neither side may name a schema or move search_path."""
    log, state = pg

    state["deny_ddl"] = False  # the indexer runs as the role that owns the schema
    indexer = RAGStore(DSN)
    indexer.connect()
    written = _tables(r"CREATE TABLE IF NOT EXISTS\s+([\w.]+)", log)
    assert {"chunks", "documents"} <= written

    log.clear()
    state["deny_ddl"] = True
    reader = _open_for_chat()
    reader.search(query_text="pump seal inspection interval", corpora=["rag-org"])
    # Catalog probes (column type, row estimate) are not corpus tables.
    read = {
        t
        for t in _tables(r"\bFROM\s+([\w.]+)", log)
        if not t.startswith(("pg_", "information_schema."))
    }
    assert "chunks" in read
    assert read <= written, f"chat read tables the indexer never wrote: {read - written}"
    assert not any("." in t for t in read | written), "a schema-qualified name splits the corpus"
    assert not any("search_path" in s.lower() for s in log)
