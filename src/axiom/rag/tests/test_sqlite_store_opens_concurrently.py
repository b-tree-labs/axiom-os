# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0
"""Opening the SQLite RAG store from several threads at once must not hit "database is locked".

CI failed on it in the library's concurrent-upload test (the default 5 s lock wait, plus a
journal-mode switch that takes a write lock the file no longer needs).
"""

from __future__ import annotations

import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from axiom.rag import sqlite_store
from axiom.rag.sqlite_store import SQLiteRAGStore


def test_a_connection_waits_long_enough_for_another_writer(tmp_path):
    store = SQLiteRAGStore(f"sqlite:///{tmp_path / 'rag.db'}")
    store.connect()
    wait_ms = store._conn.execute("PRAGMA busy_timeout").fetchone()[0]
    assert wait_ms >= 30000


def test_a_file_already_in_wal_is_not_switched_again(tmp_path):
    db = tmp_path / "rag.db"
    SQLiteRAGStore(f"sqlite:///{db}").connect()
    # Another connection holds a write lock. A second open needs no write lock to learn the
    # file is already WAL, so it reads the mode and goes on.
    holder = sqlite3.connect(str(db), isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    try:
        conn = sqlite3.connect(str(db), timeout=0.2)
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        conn.close()
    finally:
        holder.execute("ROLLBACK")
        holder.close()


def test_many_threads_open_a_fresh_file_together_round_after_round(tmp_path):
    """The schema setup reads a table's columns and then ALTERs it. On a fresh file every
    opener does that at once; a connection whose snapshot another thread has moved past
    cannot upgrade to a write, and SQLite refuses it at once ("database is locked"), however
    long the busy timeout is. CI hit it again after the timeout was raised (2026-11-23)."""
    errors = []
    for round_no in range(12):
        url = f"sqlite:///{tmp_path / f'rag{round_no}.db'}"

        def open_one(_, url=url):
            try:
                SQLiteRAGStore(url).connect()
            except sqlite3.OperationalError as exc:  # noqa: PERF203
                errors.append(str(exc))

        with ThreadPoolExecutor(max_workers=16) as pool:
            list(pool.map(open_one, range(32)))
    assert not errors, f"{len(errors)} opens failed, first: {errors[0]}"
    assert sqlite_store._LOCK_WAIT_S >= 30


def test_schema_setup_for_one_file_never_overlaps_between_threads(tmp_path, monkeypatch):
    """Deterministic form of the CI flake: two openers inside the schema block at once is
    what let one read a table's columns, lose its snapshot to the other's ALTER, and be
    refused. The block now runs one thread at a time per file."""
    inside, worst = [0], [0]
    guard = threading.Lock()
    real = sqlite_store._migrate_access_columns

    def watched(conn):
        with guard:
            inside[0] += 1
            worst[0] = max(worst[0], inside[0])
        time.sleep(0.05)  # long enough that an unserialised second opener arrives
        try:
            real(conn)
        finally:
            with guard:
                inside[0] -= 1

    monkeypatch.setattr(sqlite_store, "_migrate_access_columns", watched)
    url = f"sqlite:///{tmp_path / 'rag.db'}"
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: SQLiteRAGStore(url).connect(), range(8)))
    assert worst[0] == 1, f"{worst[0]} openers were in the schema block at once"


def test_the_switch_to_wal_happens_one_thread_at_a_time_under_the_files_lock(tmp_path, monkeypatch):
    """The other half of the CI flake (main red on 372fc01ae, 2026-11-24): on a fresh file every
    opener read the journal mode as not WAL and switched it, outside the per-file lock. The
    switch needs an exclusive lock, and a connection that cannot get one may be refused at once
    rather than waiting. So the mode is read and switched inside the lock, with the retry."""
    real_connect = sqlite3.connect
    seen = []

    def traced(*args, **kwargs):
        conn = real_connect(*args, **kwargs)

        def record(stmt):
            if "journal_mode" in stmt.lower():
                seen.append(sqlite_store._schema_lock(str(tmp_path / "rag.db")).locked())

        conn.set_trace_callback(record)
        return conn

    monkeypatch.setattr(sqlite_store.sqlite3, "connect", traced)
    SQLiteRAGStore(f"sqlite:///{tmp_path / 'rag.db'}").connect()
    assert seen and all(seen), "the journal mode was read or switched outside the file's lock"


def test_different_files_do_not_wait_for_each_other(tmp_path, monkeypatch):
    seen = []
    barrier = threading.Barrier(2, timeout=5)
    real = sqlite_store._migrate_access_columns

    def watched(conn):
        seen.append(1)
        barrier.wait()  # both files must be inside at the same time, or this times out
        real(conn)

    monkeypatch.setattr(sqlite_store, "_migrate_access_columns", watched)
    with ThreadPoolExecutor(max_workers=2) as pool:
        urls = [f"sqlite:///{tmp_path / f'f{i}.db'}" for i in range(2)]
        list(pool.map(lambda u: SQLiteRAGStore(u).connect(), urls))
    assert len(seen) == 2


def test_a_transient_lock_in_the_schema_block_is_retried(tmp_path, monkeypatch):
    real = sqlite_store._migrate_access_columns
    calls = []

    def flaky(conn):
        calls.append(1)
        if len(calls) < 3:
            raise sqlite3.OperationalError("database is locked")
        real(conn)

    monkeypatch.setattr(sqlite_store, "_migrate_access_columns", flaky)
    monkeypatch.setattr(sqlite_store, "_SCHEMA_RETRY_PAUSE_S", 0.0)
    SQLiteRAGStore(f"sqlite:///{tmp_path / 'rag.db'}").connect()
    assert len(calls) == 3


def test_a_lock_that_never_clears_still_raises(tmp_path, monkeypatch):
    def stuck(conn):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(sqlite_store, "_migrate_access_columns", stuck)
    monkeypatch.setattr(sqlite_store, "_SCHEMA_RETRY_PAUSE_S", 0.0)
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        SQLiteRAGStore(f"sqlite:///{tmp_path / 'rag.db'}").connect()


def test_another_kind_of_error_is_not_retried(tmp_path, monkeypatch):
    calls = []

    def broken(conn):
        calls.append(1)
        raise sqlite3.OperationalError("no such table: nope")

    monkeypatch.setattr(sqlite_store, "_migrate_access_columns", broken)
    monkeypatch.setattr(sqlite_store, "_SCHEMA_RETRY_PAUSE_S", 0.0)
    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        SQLiteRAGStore(f"sqlite:///{tmp_path / 'rag.db'}").connect()
    assert calls == [1]
