# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Schema-ensure must be serialized across processes.

Observed on main (2026-09-21): two xdist workers ran connect()'s DDL
script concurrently and deadlocked — AccessExclusiveLock on `chunks`
held by one, `documents` by the other, each waiting on the peer. Not a
test-only hazard: a real node's serving face, orchestrator, and CLI
connect concurrently. The fix is a Postgres advisory lock bracketing
the entire ensure-schema section, so concurrent connects serialize
(first does DDL; the rest wait briefly, then no-op). These tests pin
the bracket order with a fake connection: the advisory lock is
acquired BEFORE any DDL and released AFTER all of it, on success and
on failure alike."""

from __future__ import annotations

import pytest

from axiom.rag import store as store_mod
from axiom.rag.store import SCHEMA_ADVISORY_LOCK_KEY, RAGStore


class _FakeCursor:
    def __init__(self, log, fail_on=None):
        self.log = log
        self.fail_on = fail_on

    def execute(self, sql, params=None):
        self.log.append((sql.strip().split("(")[0].strip(), params))
        if self.fail_on and self.fail_on in sql:
            raise RuntimeError(f"boom on {self.fail_on}")

    def fetchone(self):
        return (0,)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, log, fail_on=None):
        self.log = log
        self.closed = False
        self.autocommit = False
        self._fail_on = fail_on

    def cursor(self, *a, **k):
        return _FakeCursor(self.log, self._fail_on)


@pytest.fixture()
def fake_pg(monkeypatch):
    log = []
    state = {"fail_on": None}

    def fake_connect(dsn):
        return _FakeConn(log, state["fail_on"])

    monkeypatch.setattr(store_mod.psycopg2, "connect", fake_connect)
    # _set_probes touches the fake cursor too; keep it inert.
    monkeypatch.setattr(RAGStore, "_set_probes", lambda self: None)
    return log, state


def _lock_events(log):
    return [
        (sql, params)
        for sql, params in log
        if "pg_advisory_lock" in sql or "pg_advisory_unlock" in sql
    ]


def test_ddl_is_bracketed_by_the_advisory_lock(fake_pg):
    log, _ = fake_pg
    s = RAGStore("postgresql://fake/db")
    s.connect()
    events = _lock_events(log)
    assert len(events) == 2, f"expected lock+unlock, got {events}"
    assert "pg_advisory_lock" in events[0][0]
    assert events[0][1] == (SCHEMA_ADVISORY_LOCK_KEY,)
    assert "pg_advisory_unlock" in events[1][0]
    lock_idx = next(i for i, (sql, _) in enumerate(log) if "pg_advisory_lock" in sql)
    unlock_idx = next(i for i, (sql, _) in enumerate(log) if "pg_advisory_unlock" in sql)
    ddl_idxs = [
        i for i, (sql, _) in enumerate(log) if "CREATE TABLE" in sql or "ALTER TABLE" in sql
    ]
    assert ddl_idxs, "the fake saw no DDL — the test lost its subject"
    assert all(lock_idx < i < unlock_idx for i in ddl_idxs), (
        "every DDL statement must run inside the advisory-lock bracket"
    )


def test_lock_released_even_when_ddl_fails(fake_pg):
    log, state = fake_pg
    state["fail_on"] = "CREATE TABLE"
    s = RAGStore("postgresql://fake/db")
    with pytest.raises(RuntimeError):
        s.connect()
    events = _lock_events(log)
    assert any("pg_advisory_unlock" in sql for sql, _ in events), (
        "a failed ensure must not leave the advisory lock held"
    )


def test_no_lock_when_ensure_schema_disabled(fake_pg):
    log, _ = fake_pg
    s = RAGStore("postgresql://fake/db", ensure_schema=False)
    s.connect()
    assert _lock_events(log) == []
