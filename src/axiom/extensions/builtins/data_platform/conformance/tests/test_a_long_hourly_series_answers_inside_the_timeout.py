# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A two-channel hourly series over 90 days answers well inside the read timeout (#1589).

Measured through a bound install, a 66-day hourly read of two channels timed out
at 30 s. The cost followed the window, not the bucket: a read decompresses every
raw row in its window whatever bucket it asks for.

Measured here before writing this (TimescaleDB 2.28.2, 1-day chunks compressed,
two channels every 5 s, 3.1 M rows in the window): the hourly answer took 3.4 to
4.1 s, of which about 0.7 s was the six population reads and the rest the
series itself. The plan uses the compressed segment index on (site, feed,
channel) and the chunk time bounds, so on compressed history this read is
inside the timeout without a rollup. On a node whose history is a plain table,
the same read is a range scan over every raw row, and that is what times out.

The test holds the acceptance: on compressed history, the read answers in under
a third of the default timeout.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

import pytest  # noqa: F401 - fixtures come from conftest.py

from axiom.extensions.builtins.data_platform import gold_query as gq
from axiom.extensions.builtins.data_platform.conformance import (
    GOLD_SIGNALS_DDL,
    SILVER_DDL,
)
from axiom.extensions.builtins.data_platform.conformance.timeseries import (
    TimeseriesPolicy,
    make_signals_time_partitioned,
)
from axiom.extensions.builtins.data_platform.read_target import DEFAULT_READ_TIMEOUT

DAYS = 90
CADENCE = "30 seconds"  # 518,400 readings in the window across the two channels


def _load(conn) -> datetime:
    conn.execute("DROP SCHEMA IF EXISTS gold CASCADE")
    for stmt in SILVER_DDL:
        conn.execute(stmt)
    make_signals_time_partitioned(conn, TimeseriesPolicy())
    for stmt in GOLD_SIGNALS_DDL:
        conn.execute(stmt)
    end = datetime.now(UTC).replace(microsecond=0)
    for channel in ("TC1", "TC2"):
        conn.execute(
            f"""INSERT INTO silver.signals (site, feed, channel, ts, value, unit, quality,
                    source_class, schema_ref, row_hash)
                SELECT 'site-a', 'site-a.live', %s, t, random(), 'degC', 'good',
                       'measured', 'site-a/v1', md5(%s || t::text)
                FROM generate_series(%s::timestamptz - interval '{DAYS + 2} days', %s,
                                     interval '{CADENCE}') t""",
            (channel, channel, end, end),
        )
    conn.execute(
        "SELECT compress_chunk(c) FROM show_chunks('silver.signals', "
        "older_than => interval '2 days') c"
    )
    conn.execute("ANALYZE silver.signals")
    return end


def test_two_channels_hourly_over_ninety_days_answers_well_inside_the_timeout(conn):
    end = _load(conn)
    window = {"column": "ts", "start": end - timedelta(days=DAYS), "end": end}

    started = time.monotonic()
    out = gq.series(
        conn.cursor(),
        table="signals",
        column="value",
        bucket="1 hour",
        time_column="ts",
        window=window,
        filter="feed = 'site-a.live' and channel in ('TC1', 'TC2')",
        group_by=["channel"],
    )
    took = time.monotonic() - started

    lines = out["data"]["series"]
    assert sorted(s["key"]["channel"] for s in lines) == ["TC1", "TC2"]
    assert all(DAYS * 24 <= len(s["points"]) <= DAYS * 24 + 1 for s in lines)
    assert took < DEFAULT_READ_TIMEOUT / 3, f"{took:.1f} s for a 90-day hourly read"
