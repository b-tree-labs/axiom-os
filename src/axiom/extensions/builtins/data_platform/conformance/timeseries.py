# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Make ``silver.signals`` a compressed, retained TimescaleDB hypertable.

Opt-in, for a node whose database has TimescaleDB (a producing site's local
medallion, ADR-180). A node that never calls this keeps the plain table and is
unaffected: the conform upsert detects which layout it is writing to.

Why the unique key changes. The plain table's key is ``(row_hash, channel)``.
TimescaleDB requires the partitioning column in every unique index, so the key
becomes ``(row_hash, channel, ts)``. That changes nothing about what is unique:
``row_hash`` hashes the whole bronze row, ``ts`` included, so one ``row_hash``
has one ``ts``. ``silver.signal_uncertainty`` gains ``ts`` so its foreign key
still points at exactly one reading.

Measured on a real one-day export (718,708 readings, 94 channels), 2026-10-08:

=========================  ================
layout                     bytes per reading
=========================  ================
plain table                501
hypertable, uncompressed   553
hypertable, compressed     77
=========================  ================

Compression segments by ``(site, feed, channel)`` and orders by ``ts``, which
is how readings are queried: one channel over a time window.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TimeseriesPolicy:
    #: Chunks are this many days wide.
    chunk_days: int = 1
    #: A chunk is compressed once its newest reading is this many days old.
    compress_after_days: int = 2
    #: Readings older than this many days are dropped. ``None`` keeps everything,
    #: which is the default: deleting a site's history is never implicit.
    retain_days: int | None = None


def is_time_partitioned(cur) -> bool:
    """True when ``silver.signals`` is a TimescaleDB hypertable."""
    try:
        cur.execute(
            "SELECT 1 FROM pg_extension WHERE extname = 'timescaledb'"
        )
        if cur.fetchone() is None:
            return False
        cur.execute(
            "SELECT 1 FROM timescaledb_information.hypertables "
            "WHERE hypertable_schema = 'silver' AND hypertable_name = 'signals'"
        )
        return cur.fetchone() is not None
    except Exception:  # noqa: BLE001 - an unreadable catalogue means "plain"
        return False


def _has_table(conn, schema: str, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_schema = %s AND table_name = %s",
        (schema, table),
    ).fetchone() is not None


def make_signals_time_partitioned(conn, policy: TimeseriesPolicy) -> list[str]:
    """Convert ``silver.signals`` in place and install the policy. Idempotent.

    ``conn`` must be in autocommit mode (TimescaleDB policy calls cannot run
    inside a caller's transaction). Existing rows are kept. Returns the steps
    it actually took, for the caller to report.
    """
    steps: list[str] = []
    conn.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")
    has_uncertainty = _has_table(conn, "silver", "signal_uncertainty")

    if not is_time_partitioned(conn.cursor()):
        if has_uncertainty:
            conn.execute("ALTER TABLE silver.signal_uncertainty ADD COLUMN IF NOT EXISTS ts timestamptz")
            # ADR-128 E4: the schema manager converting the tier in place.
            conn.execute(
                "UPDATE silver.signal_uncertainty u SET ts = s.ts FROM silver.signals s "
                "WHERE u.ts IS NULL AND u.row_hash = s.row_hash AND u.channel = s.channel"
            )
            conn.execute(
                "ALTER TABLE silver.signal_uncertainty "
                "DROP CONSTRAINT IF EXISTS signal_uncertainty_row_hash_channel_fkey"
            )
            conn.execute(
                "ALTER TABLE silver.signal_uncertainty "
                "DROP CONSTRAINT IF EXISTS signal_uncertainty_pkey"
            )
            conn.execute("ALTER TABLE silver.signal_uncertainty ALTER COLUMN ts SET NOT NULL")
            conn.execute(
                "ALTER TABLE silver.signal_uncertainty "
                "ADD PRIMARY KEY (row_hash, channel, ts, symbol)"
            )
            steps.append("signal_uncertainty now carries ts")
        conn.execute("ALTER TABLE silver.signals DROP CONSTRAINT IF EXISTS signals_pkey")
        conn.execute("ALTER TABLE silver.signals ADD PRIMARY KEY (row_hash, channel, ts)")
        conn.execute(
            "SELECT create_hypertable('silver.signals', 'ts', "
            "chunk_time_interval => make_interval(days => %s), migrate_data => true)",
            (policy.chunk_days,),
        )
        steps.append(f"silver.signals is a hypertable ({policy.chunk_days}-day chunks)")
        if has_uncertainty:
            # No foreign key into the hypertable. TimescaleDB 2.17.2 checks one
            # wrongly under a generic plan, which PostgreSQL picks after five
            # executions, so the sixth reading with terms in a session was
            # refused although its reading was there (reproduced 2026-10-08).
            # The terms table is partitioned and retained on the same ts
            # instead, so a reading's terms age out with it; conform writes a
            # reading's terms right after the reading, in the same transaction.
            conn.execute(
                "SELECT create_hypertable('silver.signal_uncertainty', 'ts', "
                "chunk_time_interval => make_interval(days => %s), migrate_data => true)",
                (policy.chunk_days,),
            )
            steps.append("silver.signal_uncertainty is a hypertable on the same ts")

    conn.execute(
        "ALTER TABLE silver.signals SET (timescaledb.compress, "
        "timescaledb.compress_segmentby = 'site, feed, channel', "
        "timescaledb.compress_orderby = 'ts')"
    )
    conn.execute(
        "SELECT add_compression_policy('silver.signals', make_interval(days => %s), "
        "if_not_exists => true)",
        (policy.compress_after_days,),
    )
    steps.append(f"compress chunks older than {policy.compress_after_days} days")
    if policy.retain_days is not None:
        conn.execute(
            "SELECT add_retention_policy('silver.signals', make_interval(days => %s), "
            "if_not_exists => true)",
            (policy.retain_days,),
        )
        if has_uncertainty:
            conn.execute(
                "SELECT add_retention_policy('silver.signal_uncertainty', "
                "make_interval(days => %s), if_not_exists => true)",
                (policy.retain_days,),
            )
        steps.append(f"drop readings older than {policy.retain_days} days")
    return steps


__all__ = ["TimeseriesPolicy", "is_time_partitioned", "make_signals_time_partitioned"]
