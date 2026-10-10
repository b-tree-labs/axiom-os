# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""silver.signals can be a compressed, retained TimescaleDB hypertable.

A producing site keeping its own copy (ADR-180) runs on modest hardware. On a
real export measured 2026-10-08, the plain table took 501 bytes per reading;
compressed, 77. The plain table could not be made a hypertable at all: its
unique key ``(row_hash, channel)`` omits ``ts``, and TimescaleDB requires the
partitioning column in every unique index.

``ts`` joining the key changes nothing about what is unique: ``row_hash`` is a
hash of the whole bronze row, ``ts`` included, so one ``row_hash`` has exactly
one ``ts``. The companion uncertainty table carries ``ts`` too, so a term still
names exactly one reading, and it is partitioned and retained on that ``ts`` so
terms age out with their readings. It has no foreign key into the hypertable:
TimescaleDB 2.17.2 checks one wrongly under a generic plan.

These tests run against a real TimescaleDB in a container started for the
module. Without docker they skip locally and fail on CI.
"""

from __future__ import annotations

import pytest  # noqa: F401 - fixtures come from conftest.py

from axiom.extensions.builtins.data_platform.conformance import pg_upsert
from axiom.extensions.builtins.data_platform.conformance.timeseries import (
    TimeseriesPolicy,
    is_time_partitioned,
    make_signals_time_partitioned,
)


def _row(i: int, *, channel="TC1", day="2026-09-15", value=None, terms=None) -> dict:
    import hashlib

    ts = f"{day}T13:{(i // 60) % 60:02d}:{i % 60:02d}+00:00"
    return {
        "site": "site-a", "feed": "site-a.live", "channel": channel, "ts": ts,
        "value": float(i) if value is None else value, "unit": "degC", "quality": "ok",
        "source_class": "measured", "schema_ref": "site-a/v1",
        "row_hash": hashlib.sha256(f"{ts}|{channel}".encode()).hexdigest(),
        **({"uncertainty_terms": terms} if terms else {}),
    }


def _count(conn) -> int:
    return conn.execute("SELECT count(*) FROM silver.signals").fetchone()[0]


def test_a_plain_signals_table_is_not_time_partitioned(conn):
    assert is_time_partitioned(conn.cursor()) is False


def test_it_becomes_a_hypertable_and_the_conform_upsert_still_dedupes(conn):
    make_signals_time_partitioned(conn, TimeseriesPolicy())
    assert is_time_partitioned(conn.cursor()) is True
    up = pg_upsert(conn.cursor())
    for i in range(50):
        up(_row(i))
    for i in range(50):  # a conform pass re-reads bronze it has seen
        up(_row(i))
    assert _count(conn) == 50


def test_existing_rows_are_kept_when_it_converts(conn):
    up = pg_upsert(conn.cursor())
    for i in range(30):
        up(_row(i))
    make_signals_time_partitioned(conn, TimeseriesPolicy())
    assert _count(conn) == 30
    up = pg_upsert(conn.cursor())
    up(_row(5))  # already there
    assert _count(conn) == 30


def test_converting_twice_is_a_no_op(conn):
    make_signals_time_partitioned(conn, TimeseriesPolicy())
    make_signals_time_partitioned(conn, TimeseriesPolicy())
    assert is_time_partitioned(conn.cursor()) is True


def test_compressed_chunks_take_a_fraction_of_the_space_and_still_answer(conn):
    make_signals_time_partitioned(conn, TimeseriesPolicy(compress_after_days=1))
    # Realistic volume: per-chunk overhead dominates a toy sample. Three
    # channels, a reading every second for ~5.5 hours, a slowly varying value.
    conn.execute(
        """INSERT INTO silver.signals (site, feed, channel, ts, value, unit, quality,
                                       source_class, schema_ref, row_hash)
           SELECT 'site-a', 'site-a.live', ch,
                  timestamptz '2026-09-15 13:00:00+00' + make_interval(secs => i),
                  20.0 + (i % 50) * 0.01, 'degC', 'good', 'measured', 'site-a/v1',
                  md5(ch || i::text) || md5(i::text || ch)
           FROM generate_series(0, 19999) i, unnest(array['TC1','TC2','PT1']) ch"""
    )
    conn.execute("SELECT compress_chunk(c) FROM show_chunks('silver.signals') c")
    before, after = conn.execute(
        "SELECT sum(before_compression_total_bytes), sum(after_compression_total_bytes) "
        "FROM chunk_compression_stats('silver.signals')"
    ).fetchone()
    assert after < before / 3, (before, after)
    # A compressed chunk still answers queries and still dedupes inserts.
    assert conn.execute(
        "SELECT count(*) FROM silver.signals WHERE channel = 'TC1'").fetchone()[0] == 20000
    row_hash = conn.execute(
        "SELECT row_hash FROM silver.signals WHERE channel = 'TC1' ORDER BY ts LIMIT 1").fetchone()[0]
    dup = _row(0, channel="TC1") | {"ts": "2026-09-15T13:00:00+00:00", "row_hash": row_hash}
    pg_upsert(conn.cursor())(dup)
    assert _count(conn) == 60000


def test_the_policy_installs_compression_and_retention_jobs(conn):
    make_signals_time_partitioned(conn, TimeseriesPolicy(compress_after_days=2, retain_days=400))
    jobs = {r[0]: r[1] for r in conn.execute(
        "SELECT proc_name, config FROM timescaledb_information.jobs "
        "WHERE hypertable_schema = 'silver' AND hypertable_name = 'signals'")}
    assert "policy_compression" in jobs
    assert "policy_retention" in jobs


def test_no_retention_job_when_retention_is_unset(conn):
    make_signals_time_partitioned(conn, TimeseriesPolicy(retain_days=None))
    procs = {r[0] for r in conn.execute(
        "SELECT proc_name FROM timescaledb_information.jobs "
        "WHERE hypertable_schema = 'silver' AND hypertable_name = 'signals'")}
    assert "policy_retention" not in procs


def test_uncertainty_terms_still_attach_to_their_reading(conn):
    from axiom.extensions.builtins.data_platform.conformance import SILVER_SIGNAL_UNCERTAINTY_DDL

    for stmt in SILVER_SIGNAL_UNCERTAINTY_DDL:
        conn.execute(stmt)
    make_signals_time_partitioned(conn, TimeseriesPolicy())
    up = pg_upsert(conn.cursor())
    up(_row(1, terms={"site-a:tc1:cal": 0.05}))
    up(_row(1, terms={"site-a:tc1:cal": 0.05}))  # re-read
    got = conn.execute(
        "SELECT s.channel, u.symbol, u.coefficient FROM silver.signals s "
        "JOIN silver.signal_uncertainty u USING (row_hash, channel, ts)").fetchall()
    assert got == [("TC1", "site-a:tc1:cal", 0.05)]


def test_a_rederive_updates_a_reading_in_place(conn):
    make_signals_time_partitioned(conn, TimeseriesPolicy())
    pg_upsert(conn.cursor())(_row(3) | {"unit": None})
    pg_upsert(conn.cursor(), rederive=True)(_row(3))
    assert conn.execute("SELECT unit FROM silver.signals").fetchone()[0] == "degC"
    assert _count(conn) == 1


def test_many_readings_with_terms_in_one_session(conn):
    """A foreign key into a hypertable fails under a generic plan (TimescaleDB
    2.17.2, reproduced 2026-10-08): PostgreSQL switches to one after five
    executions, so the sixth reading with uncertainty terms in a conform session
    was refused although its reading had just been written. The layout must not
    depend on which plan the server picks."""
    from axiom.extensions.builtins.data_platform.conformance import SILVER_SIGNAL_UNCERTAINTY_DDL

    for stmt in SILVER_SIGNAL_UNCERTAINTY_DDL:
        conn.execute(stmt)
    make_signals_time_partitioned(conn, TimeseriesPolicy())
    for mode in ("auto", "force_generic_plan"):
        conn.execute(f"SET plan_cache_mode = {mode}")
        up = pg_upsert(conn.cursor())
        for i in range(40):
            up(_row(i, day="2026-09-16" if mode == "auto" else "2026-09-17",
                    terms={"site-a:tc1:cal": 0.05}))
    assert conn.execute("SELECT count(*) FROM silver.signal_uncertainty").fetchone()[0] == 80


def test_retention_takes_a_readings_terms_with_it(conn):
    from axiom.extensions.builtins.data_platform.conformance import SILVER_SIGNAL_UNCERTAINTY_DDL

    for stmt in SILVER_SIGNAL_UNCERTAINTY_DDL:
        conn.execute(stmt)
    make_signals_time_partitioned(conn, TimeseriesPolicy(retain_days=400))
    procs = {r[0] for r in conn.execute(
        "SELECT hypertable_name FROM timescaledb_information.jobs "
        "WHERE hypertable_schema = 'silver' AND proc_name = 'policy_retention'")}
    assert procs == {"signals", "signal_uncertainty"}
    up = pg_upsert(conn.cursor())
    up(_row(1, day="2020-01-01", terms={"site-a:tc1:cal": 0.05}))
    up(_row(1, day="2026-09-15", terms={"site-a:tc1:cal": 0.05}))
    for table in ("signals", "signal_uncertainty"):
        conn.execute(f"SELECT drop_chunks('silver.{table}', older_than => now() - interval '400 days')")
    assert _count(conn) == 1
    assert conn.execute("SELECT count(*) FROM silver.signal_uncertainty").fetchone()[0] == 1
