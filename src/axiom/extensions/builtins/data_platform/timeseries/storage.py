# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Storage health, measured: per table and per volume (ADR-189 D6).

What an operator needs to plan a window, and what the disk alarm needs to
say *when* rather than *how full*:

- per table: bytes (table, index, total), compressed against uncompressed
  and the ratio, the oldest and newest chunk, bytes per day;
- per volume: free bytes, bytes per day, and the days of runway.

For a hypertable the daily rate comes from the chunks themselves: the bytes
of the chunks covering the last ``growth_days``, weighted by how much of each
chunk falls in that window, divided by the days actually covered. Two rates
are kept, because they answer different questions: ``raw_bytes_per_day`` is
what arrives (uncompressed), ``bytes_per_day`` is what the volume keeps once
the compression policy has run (the raw rate over the measured ratio). The
runway uses the second. That answers on the first run; the store-health
audit's sampled database growth needs twelve hours of history before it can
say anything, and it is still used when it is the larger number, because it
sees what chunks cannot (a plain table filling, bloat).

Catalogue views and TimescaleDB's size functions only; never a ``count(*)``.
Everything here takes a DB-API connection or cursor, so the same code serves
a psycopg3 verb and the SQLAlchemy-backed node-health audit.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from .contract import TimeseriesContract

#: Days of chunks the daily rate is measured over.
DEFAULT_GROWTH_DAYS = 7

#: Relations beyond the declared ones, largest first, that a report covers.
DEFAULT_TOP = 10

_INTERNAL_SCHEMAS = ("pg_catalog", "information_schema", "pg_toast", "_timescaledb_internal",
                     "_timescaledb_catalog", "_timescaledb_config", "_timescaledb_cache")


@dataclass
class TableStorage:
    qualified: str
    kind: str  # hypertable | plain | partitioned
    declared: bool = False
    rows: int = 0
    table_bytes: int = 0
    index_bytes: int = 0
    total_bytes: int = 0
    chunks: int = 0
    compressed_chunks: int = 0
    before_compression_bytes: int | None = None
    after_compression_bytes: int | None = None
    ratio: float | None = None
    oldest: datetime | None = None
    newest: datetime | None = None
    raw_bytes_per_day: float | None = None
    bytes_per_day: float | None = None
    compress_after: timedelta | None = None
    retain: timedelta | None = None
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        for k in ("oldest", "newest"):
            d[k] = getattr(self, k).isoformat() if getattr(self, k) else None
        for k in ("compress_after", "retain"):
            d[k] = int(getattr(self, k).total_seconds()) if getattr(self, k) else None
        return d


@dataclass
class VolumeStorage:
    mount: str
    total_bytes: int
    free_bytes: int
    bytes_per_day: float | None = None
    days_of_runway: float | None = None
    #: Where the rate came from: ``chunks`` or ``database growth samples``.
    source: str = "chunks"


@dataclass
class StorageReport:
    measured_at: datetime
    growth_days: int
    tables: list[TableStorage] = field(default_factory=list)
    volume: VolumeStorage | None = None
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "measured_at": self.measured_at.isoformat(),
            "growth_days": self.growth_days,
            "tables": [t.as_dict() for t in self.tables],
            "volume": asdict(self.volume) if self.volume else None,
            "notes": list(self.notes),
        }


def runway(free_bytes: int | None, bytes_per_day: float | None) -> float | None:
    """Days until the volume is full at this rate; ``None`` without a rate.
    A forecast from an assumed rate is worse than no forecast."""
    if free_bytes is None or not bytes_per_day or bytes_per_day <= 0:
        return None
    return float(free_bytes) / float(bytes_per_day)


def growth_rate(sampled: float | None, from_chunks: float | None) -> float | None:
    """The rate the alarm should use: the larger of the two it has."""
    rates = [r for r in (sampled, from_chunks) if r is not None and r > 0]
    return max(rates) if rates else None


def volume_free(conn, disk_path: str | os.PathLike | None = None) -> tuple[str, int, int] | None:
    """``(mount, total, free)`` of the database's volume, or ``None`` when no
    path is visible from here (a pod's data directory is not a host path)."""
    candidates: list[str] = [str(disk_path) if disk_path else "", os.environ.get("AXIOM_DB_DATA_PATH", "").strip()]
    try:
        cur = conn.cursor()
        cur.execute("SHOW data_directory")
        candidates.append(cur.fetchone()[0])
    except Exception:  # noqa: BLE001 - not every role may read it
        pass
    for path in candidates:
        if not path:
            continue
        try:
            usage = shutil.disk_usage(path)
        except OSError:
            continue
        return path, usage.total, usage.free
    return None


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def _timescale(cur) -> bool:
    cur.execute("SELECT 1 FROM pg_extension WHERE extname = 'timescaledb'")
    return cur.fetchone() is not None


def table_storage(
    cur, schema: str, table: str, *, declared: bool = False, now: datetime | None = None,
    growth_days: int = DEFAULT_GROWTH_DAYS,
) -> TableStorage | None:
    """One table as the catalogue and TimescaleDB describe it, or ``None``."""
    now = _aware(now) or datetime.now(UTC)
    cur.execute(
        "SELECT c.oid, c.relkind FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = %s AND c.relname = %s AND c.relkind IN ('r', 'p')",
        (schema, table),
    )
    row = cur.fetchone()
    if row is None:
        return None
    oid, relkind = row
    qualified = f"{schema}.{table}"
    timescale = _timescale(cur)
    hyper = False
    if timescale:
        cur.execute(
            "SELECT 1 FROM timescaledb_information.hypertables WHERE hypertable_schema = %s AND hypertable_name = %s",
            (schema, table),
        )
        hyper = cur.fetchone() is not None

    if hyper:
        t = TableStorage(qualified=qualified, kind="hypertable", declared=declared)
        cur.execute("SELECT approximate_row_count(%s::regclass)", (qualified,))
        t.rows = int(cur.fetchone()[0] or 0)
        cur.execute("SELECT table_bytes, index_bytes, toast_bytes, total_bytes FROM hypertable_detailed_size(%s::regclass)", (qualified,))
        tb, ib, _toast, total = cur.fetchone() or (0, 0, 0, 0)
        t.table_bytes, t.index_bytes, t.total_bytes = int(tb or 0), int(ib or 0), int(total or 0)
        cur.execute(
            "SELECT count(*), count(*) FILTER (WHERE is_compressed), min(range_start), max(range_end) "
            "FROM timescaledb_information.chunks WHERE hypertable_schema = %s AND hypertable_name = %s",
            (schema, table),
        )
        n, nc, oldest, newest = cur.fetchone()
        t.chunks, t.compressed_chunks = int(n or 0), int(nc or 0)
        t.oldest, t.newest = _aware(oldest), _aware(newest)
        cur.execute(
            "SELECT before_compression_total_bytes, after_compression_total_bytes "
            "FROM hypertable_compression_stats(%s::regclass)",
            (qualified,),
        )
        stats = cur.fetchone()
        if stats and stats[0] and stats[1]:
            t.before_compression_bytes, t.after_compression_bytes = int(stats[0]), int(stats[1])
            t.ratio = float(stats[0]) / float(stats[1])
        t.raw_bytes_per_day, t.bytes_per_day = _chunk_rates(cur, schema, table, qualified, t.ratio, now, growth_days)
        cur.execute(
            "SELECT proc_name, (config->>'compress_after')::interval, (config->>'drop_after')::interval "
            "FROM timescaledb_information.jobs WHERE hypertable_schema = %s AND hypertable_name = %s "
            "AND proc_name IN ('policy_compression', 'policy_retention')",
            (schema, table),
        )
        for proc, after, drop in cur.fetchall():
            if proc == "policy_compression":
                t.compress_after = after
            else:
                t.retain = drop
        return t

    t = TableStorage(qualified=qualified, kind="partitioned" if relkind == "p" else "plain", declared=declared)
    if relkind == "p":
        cur.execute(
            "SELECT coalesce(sum(greatest(c.reltuples, coalesce(s.n_live_tup, 0)))::bigint, 0), "
            "       coalesce(sum(pg_table_size(c.oid)), 0), coalesce(sum(pg_indexes_size(c.oid)), 0), "
            "       coalesce(sum(pg_total_relation_size(c.oid)), 0) "
            "FROM pg_partition_tree(%s) p JOIN pg_class c ON c.oid = p.relid "
            "LEFT JOIN pg_stat_user_tables s ON s.relid = c.oid WHERE p.isleaf",
            (oid,),
        )
    else:
        cur.execute(
            "SELECT greatest(c.reltuples, coalesce(s.n_live_tup, 0))::bigint, pg_table_size(c.oid), "
            "       pg_indexes_size(c.oid), pg_total_relation_size(c.oid) "
            "FROM pg_class c LEFT JOIN pg_stat_user_tables s ON s.relid = c.oid WHERE c.oid = %s",
            (oid,),
        )
    rows, tb, ib, total = cur.fetchone()
    t.rows, t.table_bytes, t.index_bytes, t.total_bytes = max(int(rows or 0), 0), int(tb or 0), int(ib or 0), int(total or 0)
    return t


def _chunk_rates(cur, schema: str, table: str, qualified: str, ratio: float | None, now: datetime,
                 growth_days: int) -> tuple[float | None, float | None]:
    """``(raw, steady)`` bytes per day over the chunks touching the window."""
    since = now - timedelta(days=growth_days)
    cur.execute(
        "SELECT c.range_start, c.range_end, c.is_compressed, ds.total_bytes, "
        "       cs.before_compression_total_bytes, cs.after_compression_total_bytes "
        "FROM timescaledb_information.chunks c "
        "JOIN chunks_detailed_size(%s::regclass) ds USING (chunk_schema, chunk_name) "
        "LEFT JOIN chunk_compression_stats(%s::regclass) cs USING (chunk_schema, chunk_name) "
        "WHERE c.hypertable_schema = %s AND c.hypertable_name = %s AND c.range_end > %s",
        (qualified, qualified, schema, table, since),
    )
    raw = steady = 0.0
    covered_lo, covered_hi = None, None
    for start, end, compressed, total, before, after in cur.fetchall():
        start, end = _aware(start), _aware(end)
        lo, hi = max(start, since), min(end, now)
        if hi <= lo:
            continue
        weight = (hi - lo).total_seconds() / max((end - start).total_seconds(), 1.0)
        on_disk = float(total or 0)
        uncompressed = float(before) if compressed and before else on_disk
        kept = float(after) if compressed and after else (on_disk / ratio if ratio else on_disk)
        raw += uncompressed * weight
        steady += kept * weight
        covered_lo = lo if covered_lo is None or lo < covered_lo else covered_lo
        covered_hi = hi if covered_hi is None or hi > covered_hi else covered_hi
    if covered_lo is None or covered_hi is None:
        return None, None
    days = max((covered_hi - covered_lo).total_seconds() / 86400.0, 1 / 24)
    return raw / days, steady / days


def report(
    conn,
    contracts: list[TimeseriesContract],
    *,
    top: int = DEFAULT_TOP,
    disk_path: str | os.PathLike | None = None,
    growth_days: int = DEFAULT_GROWTH_DAYS,
    db_growth_bytes_per_day: float | None = None,
    now: datetime | None = None,
) -> StorageReport:
    """Declared tables first, then every hypertable, then the ``top`` largest
    other relations; and the volume with its runway."""
    now = _aware(now) or datetime.now(UTC)
    cur = conn.cursor()
    rep = StorageReport(measured_at=now, growth_days=growth_days)
    seen: set[str] = set()
    for c in contracts:
        t = table_storage(cur, c.schema, c.table, declared=True, now=now, growth_days=growth_days)
        if t is None:
            rep.notes.append(f"{c.qualified} is declared and does not exist")
            continue
        seen.add(t.qualified)
        rep.tables.append(t)
    if _timescale(cur):
        cur.execute("SELECT hypertable_schema, hypertable_name FROM timescaledb_information.hypertables ORDER BY 1, 2")
        for schema, table in cur.fetchall():
            if f"{schema}.{table}" in seen or schema in _INTERNAL_SCHEMAS:
                continue
            t = table_storage(cur, schema, table, now=now, growth_days=growth_days)
            if t is not None:
                seen.add(t.qualified)
                rep.tables.append(t)
    if top > 0:
        cur.execute(
            "SELECT n.nspname, c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE c.relkind IN ('r', 'p') AND n.nspname <> ALL(%s) "
            "AND NOT EXISTS (SELECT 1 FROM pg_inherits i WHERE i.inhrelid = c.oid) "
            "ORDER BY pg_total_relation_size(c.oid) DESC LIMIT %s",
            (list(_INTERNAL_SCHEMAS), top + len(seen)),
        )
        added = 0
        for schema, table in cur.fetchall():
            if added >= top:
                break
            if f"{schema}.{table}" in seen:
                continue
            t = table_storage(cur, schema, table, now=now, growth_days=growth_days)
            if t is not None:
                seen.add(t.qualified)
                rep.tables.append(t)
                added += 1
    chunk_rate = sum(t.bytes_per_day for t in rep.tables if t.kind == "hypertable" and t.bytes_per_day) or None
    vol = volume_free(conn, disk_path)
    if vol is None:
        rep.notes.append("the database volume is not visible from here: set AXIOM_DB_DATA_PATH or pass disk_path")
    else:
        mount, total, free = vol
        rate, source = chunk_rate, "chunks"
        if db_growth_bytes_per_day is not None and db_growth_bytes_per_day > (chunk_rate or 0):
            rate, source = db_growth_bytes_per_day, "database growth samples"
        rep.volume = VolumeStorage(mount=mount, total_bytes=total, free_bytes=free, bytes_per_day=rate,
                                   days_of_runway=runway(free, rate), source=source)
    return rep


def timeseries_growth(conn, *, growth_days: int = DEFAULT_GROWTH_DAYS, now: datetime | None = None) -> float | None:
    """Bytes per day every hypertable on this database keeps, from its chunks;
    ``None`` without TimescaleDB or without chunks in the window. Never
    raises: this feeds a health check that must still run its other checks."""
    try:
        rep = report(conn, [], top=0, disk_path=None, growth_days=growth_days, now=now)
    except Exception:  # noqa: BLE001 - see the docstring
        return None
    rates = [t.bytes_per_day for t in rep.tables if t.kind == "hypertable" and t.bytes_per_day]
    return sum(rates) if rates else None


__all__ = [
    "DEFAULT_GROWTH_DAYS",
    "DEFAULT_TOP",
    "StorageReport",
    "TableStorage",
    "VolumeStorage",
    "growth_rate",
    "report",
    "runway",
    "table_storage",
    "timeseries_growth",
    "volume_free",
]
