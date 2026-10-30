# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Reconcile compares a site's copy with the host's and never decides a value (ADR-180 §5).

Per window of (site, feed, channel, source_class, hour): a count and a content
hash. Only a window that differs is compared row by row, on the reading
identity (site, feed, channel, ts, source_class):

* a reading on one side only is a gap, filled in the direction the record of
  truth allows;
* a window the site's own retention removed is "expired locally", never a
  deletion sent upstream;
* the same identity with a different value is a conflict: both values are
  kept, the window is reported, nothing is overwritten.

Two real databases stand in for the two nodes.
"""

from __future__ import annotations

import hashlib

import psycopg
import pytest

from axiom.extensions.builtins.data_platform.conformance import SILVER_SIGNALS_DDL, pg_upsert
from axiom.extensions.builtins.data_platform.conformance.reconcile import (
    PgSide,
    reconcile,
)

T0 = "2026-10-08T10:00:00+00:00"


@pytest.fixture
def sides(timescale_dsn):
    made = []
    for name in ("recon_local", "recon_upstream"):
        with psycopg.connect(timescale_dsn, autocommit=True) as admin:
            admin.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
            admin.execute(f"CREATE DATABASE {name}")
        dsn = timescale_dsn.rsplit("/", 1)[0] + "/" + name
        c = psycopg.connect(dsn, autocommit=True)
        for stmt in SILVER_SIGNALS_DDL:
            c.execute(stmt)
        made.append(c)
    yield made[0], made[1]
    for c in made:
        c.close()


def _row(i: int, *, value=None, channel="TC1", hour=10, source_class="measured") -> dict:
    ts = f"2026-10-08T{hour:02d}:{i // 60:02d}:{i % 60:02d}+00:00"
    return {
        "site": "site-a", "feed": "site-a.live", "channel": channel, "ts": ts,
        "value": float(i) if value is None else value, "unit": "degC", "quality": "good",
        "source_class": source_class, "schema_ref": "site-a/v1",
        "row_hash": hashlib.sha256(f"{ts}|{channel}|{source_class}".encode()).hexdigest(),
    }


def _load(conn, rows):
    up = pg_upsert(conn.cursor())
    for r in rows:
        up(r)


def _count(conn, **where) -> int:
    clause = " AND ".join(f"{k} = %s" for k in where) or "true"
    return conn.execute(f"SELECT count(*) FROM silver.signals WHERE {clause}",
                        tuple(where.values())).fetchone()[0]


def _run(local, upstream, **kw):
    kw.setdefault("record_of_truth", "upstream")
    return reconcile(PgSide(local), PgSide(upstream), site="site-a",
                     start="2026-10-08T00:00:00+00:00", end="2026-10-09T00:00:00+00:00", **kw)


def test_identical_copies_compare_by_window_only(sides):
    local, upstream = sides
    rows = [_row(i) for i in range(120)]
    _load(local, rows)
    _load(upstream, rows)
    r = _run(local, upstream)
    assert r.windows_compared == 1 and r.windows_differing == 0
    assert r.filled_local == r.filled_upstream == r.conflicts == 0


def test_a_gap_upstream_is_filled_from_the_site(sides):
    local, upstream = sides
    _load(local, [_row(i) for i in range(120)])
    _load(upstream, [_row(i) for i in range(100)])
    r = _run(local, upstream)
    assert r.filled_upstream == 20
    assert _count(upstream) == 120
    assert _run(local, upstream).windows_differing == 0  # converged


def test_a_gap_at_the_site_is_refilled_from_the_host_in_contributor_mode(sides):
    # A replaced collector host, or a disk lost at the site: the shared record refills it.
    local, upstream = sides
    _load(local, [_row(i) for i in range(50)])
    _load(upstream, [_row(i) for i in range(120)])
    r = _run(local, upstream, record_of_truth="upstream")
    assert r.filled_local == 70
    assert _count(local) == 120


def test_local_first_does_not_take_the_hosts_copy_unless_the_site_opts_in(sides):
    local, upstream = sides
    _load(local, [_row(i) for i in range(50)])
    _load(upstream, [_row(i) for i in range(120)])
    r = _run(local, upstream, record_of_truth="local")
    assert r.filled_local == 0 and r.not_filled_by_choice == 70
    assert _count(local) == 50
    r = _run(local, upstream, record_of_truth="local", accept_upstream_fill=True)
    assert r.filled_local == 70


def test_local_first_fills_the_host_only_with_what_the_site_shares(sides):
    local, upstream = sides
    _load(local, [_row(i, channel="TC1") for i in range(10)] + [_row(i, channel="TC2") for i in range(10)])
    r = _run(local, upstream, record_of_truth="local",
             may_share=lambda row: row["channel"] == "TC1")
    assert r.filled_upstream == 10
    assert _count(upstream, channel="TC2") == 0


def test_a_window_the_sites_retention_removed_is_expired_not_a_deletion(sides):
    local, upstream = sides
    _load(upstream, [_row(i, hour=2) for i in range(30)] + [_row(i, hour=10) for i in range(30)])
    _load(local, [_row(i, hour=10) for i in range(30)])
    r = _run(local, upstream, record_of_truth="upstream",
             local_retained_from="2026-10-08T05:00:00+00:00")
    assert r.expired_locally == 30
    assert r.filled_local == 0  # retention said those hours are gone on purpose
    assert _count(upstream) == 60  # and nothing was deleted at the host


def test_a_different_value_at_the_same_reading_is_flagged_and_left_alone(sides):
    local, upstream = sides
    _load(local, [_row(i) for i in range(10)])
    _load(upstream, [_row(i) for i in range(10)][:5] + [_row(5, value=999.0)] + [_row(i) for i in range(6, 10)])
    r = _run(local, upstream)
    assert r.conflicts == 1
    c = r.conflict_list[0]
    assert c["channel"] == "TC1" and c["local_value"] == 5.0 and c["upstream_value"] == 999.0
    assert local.execute("SELECT value FROM silver.signals WHERE value = 5.0").fetchone()
    assert upstream.execute("SELECT value FROM silver.signals WHERE value = 999.0").fetchone()
    got = local.execute("SELECT channel, local_value, upstream_value FROM silver.reading_conflicts").fetchall()
    assert got == [("TC1", 5.0, 999.0)]


def test_measured_and_simulated_at_the_same_instant_are_not_a_conflict(sides):
    local, upstream = sides
    _load(local, [_row(0, source_class="measured", value=1.0)])
    _load(upstream, [_row(0, source_class="measured", value=1.0), _row(0, source_class="simulated", value=7.0)])
    r = _run(local, upstream, record_of_truth="local")
    assert r.conflicts == 0


def test_a_dry_run_reports_and_writes_nothing(sides):
    local, upstream = sides
    _load(local, [_row(i) for i in range(20)])
    r = _run(local, upstream, dry_run=True)
    assert r.filled_upstream == 20 and _count(upstream) == 0


def test_it_works_the_same_on_compressed_hypertables(sides):
    from axiom.extensions.builtins.data_platform.conformance.timeseries import (
        TimeseriesPolicy,
        make_signals_time_partitioned,
    )

    local, upstream = sides
    for c in sides:
        make_signals_time_partitioned(c, TimeseriesPolicy())
    _load(local, [_row(i) for i in range(120)])
    _load(upstream, [_row(i) for i in range(100)] + [_row(100, value=-1.0)])
    for c in sides:
        c.execute("SELECT compress_chunk(ch) FROM show_chunks('silver.signals') ch")
    r = _run(local, upstream)
    assert r.filled_upstream == 19 and r.conflicts == 1
    assert _count(upstream) == 120
