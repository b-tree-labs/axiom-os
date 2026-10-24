# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Storage health is measured, per table and per volume, and feeds the alarm.

Bytes, compressed against uncompressed, the ratio, the oldest chunk, bytes
per day and days of runway. For a hypertable the daily rate comes from the
chunks themselves, so it answers on the first run rather than after twelve
hours of samples. Against a real TimescaleDB (a ``tsdb-*`` container).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from axiom.extensions.builtins.data_platform.timeseries import storage
from axiom.extensions.builtins.data_platform.timeseries.contract import TimeseriesContract, apply

from .conftest import SCHEMA

D = timedelta(days=1)
GIB = 2**30


def _contract(table="readings", **over) -> TimeseriesContract:
    base = dict(schema=SCHEMA, table=table, identity=("site", "feed", "channel"), chunk_interval=D)
    base.update(over)
    return TimeseriesContract(**base)


def _create(db, table="readings"):
    db.execute(
        f"CREATE TABLE {SCHEMA}.{table} (site text NOT NULL, feed text NOT NULL, channel text NOT NULL, "
        f"ts timestamptz NOT NULL, value double precision, UNIQUE (site, feed, channel, ts))"
    )


def _fill(db, table: str, *, days: int, rows_per_day: int, end: datetime):
    """``days`` days ending at ``end``, ``rows_per_day`` rows each, two channels."""
    step = 86400 / (rows_per_day / 2)
    db.execute(
        f"INSERT INTO {SCHEMA}.{table} SELECT 'a', 'f', ch, %s - make_interval(secs => i * %s), "
        f"20.0 + (i %% 50) * 0.01 FROM generate_series(0, %s) i, unnest(array['c1','c2']) ch",
        (end, step, int(days * rows_per_day / 2) - 1),
    )
    db.execute(f"ANALYZE {SCHEMA}.{table}")


def _hypertable(db, *, days=10, rows_per_day=20_000, compress_before_days=3):
    _create(db)
    apply(db, _contract())
    now = datetime.now(UTC)
    _fill(db, "readings", days=days, rows_per_day=rows_per_day, end=now - timedelta(hours=2))
    db.execute(
        "SELECT compress_chunk(format('%%I.%%I', chunk_schema, chunk_name)::regclass, if_not_compressed => true) "
        "FROM timescaledb_information.chunks WHERE hypertable_schema = %s AND hypertable_name = 'readings' "
        "AND range_end < %s",
        (SCHEMA, now - compress_before_days * D),
    )
    return now


def test_a_hypertable_reports_bytes_ratio_oldest_chunk_and_a_daily_rate(db):
    now = _hypertable(db)
    t = storage.table_storage(db.cursor(), SCHEMA, "readings", declared=True, now=now)
    assert t.kind == "hypertable" and t.declared
    assert 150_000 < t.rows < 250_000, "an estimate, never a count"
    assert t.total_bytes > 0 and t.table_bytes > 0 and t.index_bytes > 0
    assert 10 <= t.chunks <= 11 and 6 <= t.compressed_chunks < t.chunks
    assert t.ratio and t.ratio > 2, t.ratio
    assert t.before_compression_bytes > t.after_compression_bytes
    oldest = db.execute(
        "SELECT min(range_start) FROM timescaledb_information.chunks WHERE hypertable_schema = %s "
        "AND hypertable_name = 'readings'", (SCHEMA,)).fetchone()[0]
    assert t.oldest == oldest and t.newest is not None
    assert t.raw_bytes_per_day and t.bytes_per_day
    assert t.bytes_per_day < t.raw_bytes_per_day, "steady state is the compressed rate"
    # The raw rate is what one day of chunks holds uncompressed, within the
    # slack of chunk overhead and the window's edges.
    total_raw = db.execute(
        "SELECT sum(coalesce(cs.before_compression_total_bytes, ds.total_bytes)) "
        f"FROM chunks_detailed_size('{SCHEMA}.readings') ds "
        f"LEFT JOIN chunk_compression_stats('{SCHEMA}.readings') cs USING (chunk_schema, chunk_name)"
    ).fetchone()[0]
    per_day = float(total_raw) / 10
    assert 0.5 < t.raw_bytes_per_day / per_day < 2.0, (t.raw_bytes_per_day, per_day)
    assert t.compress_after == 7 * D and t.retain is None


def test_a_plain_table_is_reported_without_a_ratio_or_a_rate(db):
    _create(db, "events")
    _fill(db, "events", days=3, rows_per_day=5_000, end=datetime.now(UTC))
    t = storage.table_storage(db.cursor(), SCHEMA, "events")
    assert t.kind == "plain" and not t.declared
    assert t.rows > 10_000 and t.total_bytes > 0
    assert t.chunks == 0 and t.ratio is None and t.bytes_per_day is None and t.oldest is None


def test_a_natively_partitioned_parent_is_sized_from_its_leaves(db):
    db.execute(f"CREATE TABLE {SCHEMA}.parts (ts timestamptz NOT NULL, v double precision) PARTITION BY RANGE (ts)")
    db.execute(f"CREATE TABLE {SCHEMA}.parts_default PARTITION OF {SCHEMA}.parts DEFAULT")
    db.execute(f"INSERT INTO {SCHEMA}.parts SELECT now() - make_interval(secs => i), i FROM generate_series(0, 49999) i")
    db.execute(f"ANALYZE {SCHEMA}.parts")
    t = storage.table_storage(db.cursor(), SCHEMA, "parts")
    assert t.kind == "partitioned" and 45_000 < t.rows < 55_000 and t.total_bytes > 0


def test_a_table_that_does_not_exist_is_none(db):
    assert storage.table_storage(db.cursor(), SCHEMA, "nothing") is None


def test_the_report_lists_declared_tables_first_then_the_largest(db, tmp_path):
    now = _hypertable(db)
    _create(db, "big")
    _fill(db, "big", days=2, rows_per_day=100_000, end=now)
    _create(db, "small")
    _fill(db, "small", days=1, rows_per_day=100, end=now)
    rep = storage.report(db, [_contract()], top=1, disk_path=str(tmp_path), now=now)
    names = [t.qualified for t in rep.tables]
    assert names[0] == f"{SCHEMA}.readings" and rep.tables[0].declared
    assert f"{SCHEMA}.big" in names and f"{SCHEMA}.small" not in names
    assert all(not t.qualified.startswith("_timescaledb_internal") for t in rep.tables)
    assert rep.volume is not None and rep.volume.free_bytes > 0
    assert rep.volume.bytes_per_day == rep.tables[0].bytes_per_day
    assert rep.volume.days_of_runway == rep.volume.free_bytes / rep.volume.bytes_per_day
    assert rep.volume.source == "chunks"
    d = rep.as_dict()
    assert d["tables"][0]["qualified"] == f"{SCHEMA}.readings" and d["volume"]["days_of_runway"] > 0


def test_a_measured_database_growth_wins_when_it_is_larger(db, tmp_path):
    """The sampled database growth includes everything the chunks cannot see
    (a plain table filling, bloat); when it is the larger number it is the
    one the runway is computed from, and the report says which it used."""
    now = _hypertable(db)
    rep = storage.report(db, [_contract()], disk_path=str(tmp_path), now=now,
                         db_growth_bytes_per_day=rep_rate(db, now) * 10)
    assert rep.volume.source == "database growth samples"
    assert rep.volume.days_of_runway < storage.report(
        db, [_contract()], disk_path=str(tmp_path), now=now).volume.days_of_runway
    # Negative control: a smaller sample does not override the chunks.
    smaller = storage.report(db, [_contract()], disk_path=str(tmp_path), now=now,
                             db_growth_bytes_per_day=rep_rate(db, now) / 10)
    assert smaller.volume.source == "chunks"


def rep_rate(db, now) -> float:
    return storage.table_storage(db.cursor(), SCHEMA, "readings", now=now).bytes_per_day


def test_runway_is_free_space_over_the_rate_and_silent_without_a_rate():
    assert storage.runway(100 * GIB, 1 * GIB) == 100
    assert storage.runway(100 * GIB, 0) is None
    assert storage.runway(100 * GIB, None) is None
    assert storage.runway(0, 1 * GIB) == 0


def test_the_alarm_gets_the_chunk_rate_when_the_samples_cannot_say_yet(db):
    """``days_to_full`` stayed silent until twelve hours of samples existed.
    The chunks answer at once; a measured sample still wins when it is larger."""
    now = _hypertable(db)
    chunk_rate = storage.timeseries_growth(db, now=now)
    assert chunk_rate and chunk_rate > 0
    assert storage.growth_rate(None, chunk_rate) == chunk_rate
    assert storage.growth_rate(chunk_rate * 3, chunk_rate) == chunk_rate * 3
    assert storage.growth_rate(chunk_rate / 3, chunk_rate) == chunk_rate
    assert storage.growth_rate(None, None) is None
    assert storage.growth_rate(5.0, None) == 5.0


def test_a_database_without_timescaledb_reports_plain_tables_and_no_chunk_rate(db):
    """Negative control at the volume: with no hypertable there is no chunk
    rate, the volume says so, and nothing raises."""
    _create(db, "events")
    rep = storage.report(db, [], top=5, disk_path=None, now=datetime.now(UTC))
    assert all(t.kind != "hypertable" for t in rep.tables)
    assert storage.timeseries_growth(db) is None or storage.timeseries_growth(db) == 0.0
