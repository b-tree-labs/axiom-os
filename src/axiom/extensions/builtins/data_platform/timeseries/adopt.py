# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Adopt a populated plain table into the time-series contract, in place.

``create_hypertable(migrate_data => true)`` holds a lock for the length of a
full copy. On the 466 GB table that motivated this (ADR-189) that is hours,
and ADR-182 forbids an outage from our own causes. So the conversion is done
beside the table, with the writer running, in resumable steps:

1. **Estimate** (:func:`estimate`; writes nothing). Rows and bytes from the
   catalogue (a natively partitioned parent is sized from its partitions),
   the time range, what would block the adoption (a unique index without the
   time column, a foreign key that would follow the old table through the
   rename, a trigger, no index that can range on time), the dependent views
   that will be rebound, and the expected ratio measured on a sample copied
   into a scratch hypertable and compressed. From these: the windows, the
   bytes needed, and an ETA from the sample's measured rate.
2. **Copy by time window** (:func:`run`) into ``<table>__adopt``, a
   hypertable created from the contract, oldest first, one window per
   transaction. Each window is **verified before it is marked done**: the
   row count and an order-independent hash (the sum of a 64-bit prefix of
   each row's digest) must agree between source and copy. A mismatch rolls
   the window back and stops with the window named. Verified chunks are
   compressed as the copy advances, so the copy needs compressed space, not
   a second copy of the table. Progress lives in a ledger in the database,
   written in the same transaction as the window, so a fresh call resumes at
   the first window not done.
3. **Swap** (:func:`swap`) in one short transaction: the tail that arrived
   since the last window is copied under the only exclusive lock taken, the
   names swap, dependent views are recreated so they bind to the new table
   (a view follows the old table's identity through a rename), privileges
   and sequences are carried over. The old table stays, as ``<table>__old``.
4. **Verify** (:func:`verify`): every window of the old table against the
   new one. A window whose digest differs is resolved exactly (``EXCEPT
   ALL``): rows the new table lacks are a loss, reported and, with
   ``repair``, re-inserted from the old table; rows written to the new table
   since the swap are not.
5. **Drop** (:func:`drop_old`) only after a verification passed, and
   **rollback** (:func:`rollback`) at any point before it.

Nothing here knows what the table holds. The time column, the identity and
the rate come from the contract.
"""

from __future__ import annotations

import logging
import shutil
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from .contract import (
    DEFAULT_LOCK_TIMEOUT,
    TimeseriesContract,
    apply,
    status,
    unique_indexes_missing_time,
)

log = logging.getLogger(__name__)

#: The ledger lives in this extension's own schema (ADR-052).
LEDGER_SCHEMA = "data_platform"
ADOPTIONS = f"{LEDGER_SCHEMA}.timeseries_adoptions"
WINDOWS = f"{LEDGER_SCHEMA}.timeseries_adoption_windows"

NEW_SUFFIX = "__adopt"
OLD_SUFFIX = "__old"
SAMPLE_SUFFIX = "__sample"

#: Rows copied into the scratch hypertable to measure the ratio and the rate.
DEFAULT_SAMPLE_ROWS = 200_000

#: Attempts to take the swap's exclusive lock before giving up (each bounded
#: by ``lock_timeout``), so a long-running reader delays the swap rather than
#: the swap stalling the writer.
SWAP_ATTEMPTS = 12

#: Attempts at one window before the copy stops: a window that keeps
#: changing under the copy (late rows) is retried, a window that keeps
#: disagreeing is reported.
WINDOW_ATTEMPTS = 3

#: Headroom over the expected compressed size the volume must have before the
#: copy starts: the newest window sits uncompressed with its indexes, and
#: the ratio is a sample.
FREE_SPACE_FACTOR = 2.0

PHASES = ("none", "copying", "swapped", "verified", "dropped", "rolled_back")


class AdoptionRefused(Exception):
    """The step cannot run safely as things stand; the message says why."""


class AdoptionBusy(AdoptionRefused):
    """Another process is copying this table right now (single-flight)."""


class PartitionRefused(AdoptionRefused):
    """The target is a partition (or an inheritance child) of another table.

    Readers and writers go through the parent, and a TimescaleDB hypertable
    cannot be attached to it, so swapping the partition for a hypertable would
    leave every one of them on the old table. The message names the parent and
    the command that adopts it instead."""


class SwapUnsafe(AdoptionRefused):
    """After renaming, a read would still land on the old table. The swap was
    rolled back inside its own transaction; nothing changed."""


def _lock_name(qualified: str) -> str:
    """The single-flight name for a table's adoption (one scheme, ADR-197 D7)."""
    return f"timeseries.adopt:{qualified}"


_LEDGER_DDL = (
    f"CREATE SCHEMA IF NOT EXISTS {LEDGER_SCHEMA}",
    f"""CREATE TABLE IF NOT EXISTS {ADOPTIONS} (
        qualified text PRIMARY KEY,
        phase text NOT NULL,
        window_seconds integer NOT NULL,
        range_lo timestamptz,
        range_hi timestamptz,
        boundary timestamptz,
        rows_estimate bigint NOT NULL DEFAULT 0,
        tail_from timestamptz,
        started_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now(),
        swapped_at timestamptz,
        verified_at timestamptz,
        notes jsonb NOT NULL DEFAULT '{{}}'::jsonb
    )""",
    f"""CREATE TABLE IF NOT EXISTS {WINDOWS} (
        qualified text NOT NULL,
        window_start timestamptz NOT NULL,
        window_end timestamptz NOT NULL,
        rows bigint NOT NULL,
        digest numeric NOT NULL,
        seconds double precision NOT NULL,
        done_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (qualified, window_start)
    )""",
)


def _ensure_ledger(conn) -> None:
    for stmt in _LEDGER_DDL:
        conn.execute(stmt)


def _q(schema: str, name: str) -> str:
    return f'"{schema}"."{name}"'


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def align_down(moment: datetime, window: timedelta) -> datetime:
    """The window boundary at or before ``moment``, on the same epoch grid
    TimescaleDB aligns chunks to, so a window is whole chunks."""
    secs = int(window.total_seconds())
    epoch = int(_aware(moment).timestamp())
    return datetime.fromtimestamp(epoch - (epoch % secs), UTC)


def align_up(moment: datetime, window: timedelta) -> datetime:
    down = align_down(moment, window)
    return down if down == _aware(moment) else down + window


def plan_windows(lo: datetime, hi: datetime, window: timedelta) -> list[tuple[datetime, datetime]]:
    """Half-open windows ``[a, b)`` covering ``[lo, hi)``, aligned. Pure."""
    out = []
    start = align_down(lo, window)
    end = align_up(hi, window)
    while start < end:
        out.append((start, start + window))
        start += window
    return out


# -- facts about the source ---------------------------------------------------


def root_parent(cur, oid: int) -> tuple[str | None, bool]:
    """``(schema.name of the root of the tree the relation hangs from, whether
    it is a declarative partition)``; ``(None, False)`` for a relation that is
    nobody's child. The root, not the nearest parent: a partition of a
    sub-partitioned partition is adopted by adopting the whole tree."""
    cur.execute(
        "WITH RECURSIVE up(rel, depth) AS ("
        "  SELECT inhparent, 1 FROM pg_inherits WHERE inhrelid = %s"
        "  UNION ALL SELECT i.inhparent, up.depth + 1 FROM pg_inherits i JOIN up ON i.inhrelid = up.rel) "
        "SELECT n.nspname || '.' || c.relname, (SELECT relispartition FROM pg_class WHERE oid = %s) "
        "FROM up JOIN pg_class c ON c.oid = up.rel JOIN pg_namespace n ON n.oid = c.relnamespace "
        "ORDER BY up.depth DESC LIMIT 1",
        (oid, oid),
    )
    row = cur.fetchone()
    return (row[0], bool(row[1])) if row else (None, False)


def _adopt_command(parent: str, contract: TimeseriesContract) -> str:
    parts = [f"axi data adopt-timeseries {parent}"]
    if contract.identity:
        parts.append("--identity " + ",".join(contract.identity))
    if contract.time_column != "ts":
        parts.append(f"--time-column {contract.time_column}")
    return " ".join(parts)


def partition_refusal(facts: SourceFacts, contract: TimeseriesContract) -> str | None:
    """Why this target must not be adopted on its own, or None."""
    if not facts.parent:
        return None
    kind = "a partition" if facts.parent_is_partition else "an inheritance child"
    return (
        f"{contract.qualified} is {kind} of {facts.parent}, and every reader and writer goes through "
        f"{facts.parent}. A TimescaleDB hypertable cannot be attached to it, so adopting {contract.table} "
        f"alone would swap the name and leave {facts.parent} reading and writing the old table. "
        f"Adopt the parent instead: {_adopt_command(facts.parent, contract)}"
    )


def _refuse_partition(conn, contract: TimeseriesContract) -> None:
    """Raise :class:`PartitionRefused` when the target hangs from a parent.
    Checked on every call that moves an adoption forward (a ledger begun before
    the rule existed is stopped too); never on rollback, which is how one leaves."""
    cur = conn.cursor()
    cur.execute(
        "SELECT c.oid FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = %s AND c.relname = %s", (contract.schema, contract.table))
    row = cur.fetchone()
    if row is None:
        return
    parent, is_partition = root_parent(cur, row[0])
    facts = SourceFacts(exists=True, parent=parent, parent_is_partition=is_partition)
    message = partition_refusal(facts, contract)
    if message:
        raise PartitionRefused(message)


@dataclass
class SourceFacts:
    exists: bool = False
    relkind: str = ""
    rows: int = 0
    bytes: int = 0
    is_hypertable: bool = False
    time_index: str | None = None
    """An index whose leading column is the time column (btree or BRIN), which
    is what makes a window range scan cheap. None when there is none."""
    time_btree: bool = False
    """That leading index is a btree, so min/max of the time column is instant."""
    unique_missing_time: list[str] = field(default_factory=list)
    referencing_fks: list[str] = field(default_factory=list)
    triggers: list[str] = field(default_factory=list)
    dependent_views: list[str] = field(default_factory=list)
    materialized_views: list[str] = field(default_factory=list)
    partitions: int = 0
    partitions_without_time_index: list[str] = field(default_factory=list)
    """For a partitioned parent whose own index does not lead with the time
    column: the leaf partitions that have none either."""
    parent: str | None = None
    """The root of the tree this table is a partition (or inheritance child) of,
    schema-qualified; None when it is not one."""
    parent_is_partition: bool = False


def source_facts(conn, contract: TimeseriesContract) -> SourceFacts:
    cur = conn.cursor()
    cur.execute(
        "SELECT c.oid, c.relkind FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = %s AND c.relname = %s",
        (contract.schema, contract.table),
    )
    row = cur.fetchone()
    if row is None:
        return SourceFacts()
    oid, relkind = row
    f = SourceFacts(exists=True, relkind=relkind)
    f.is_hypertable = status(conn, contract).is_hypertable
    f.parent, f.parent_is_partition = root_parent(cur, oid)
    # A natively partitioned parent holds nothing itself, and since PG 14 its
    # ANALYZE'd reltuples is the whole tree's, so it is sized from its leaves
    # alone; a plain table is sized from itself.
    if relkind == "p":
        cur.execute(
            "SELECT count(*), "
            "       coalesce(sum(greatest(c.reltuples, coalesce(s.n_live_tup, 0)))::bigint, 0), "
            "       coalesce(sum(pg_total_relation_size(c.oid)), 0) "
            "FROM pg_partition_tree(%s) t JOIN pg_class c ON c.oid = t.relid "
            "LEFT JOIN pg_stat_user_tables s ON s.relid = c.oid WHERE t.isleaf",
            (oid,),
        )
        parts, rows, total = cur.fetchone()
        f.partitions = int(parts or 0)
    else:
        cur.execute(
            "SELECT greatest(c.reltuples, coalesce(s.n_live_tup, 0))::bigint, pg_total_relation_size(c.oid) "
            "FROM pg_class c LEFT JOIN pg_stat_user_tables s ON s.relid = c.oid WHERE c.oid = %s",
            (oid,),
        )
        rows, total = cur.fetchone()
    f.rows, f.bytes = max(int(rows or 0), 0), int(total or 0)
    cur.execute(
        "SELECT i.indexrelid::regclass::text, am.amname FROM pg_index i "
        "JOIN pg_class ic ON ic.oid = i.indexrelid JOIN pg_am am ON am.oid = ic.relam "
        "JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = i.indkey[0] "
        "WHERE i.indrelid = %s AND a.attname = %s ORDER BY (am.amname = 'btree') DESC",
        (oid, contract.time_column),
    )
    idx = cur.fetchone()
    if idx:
        f.time_index, f.time_btree = idx[0], idx[1] == "btree"
    elif relkind == "p":
        # The parent's index is optional; what a window needs is an index on
        # every leaf it reads.
        cur.execute(
            "SELECT t.relid::regclass::text, (SELECT am.amname FROM pg_index i "
            "JOIN pg_class ic ON ic.oid = i.indexrelid JOIN pg_am am ON am.oid = ic.relam "
            "JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = i.indkey[0] "
            "WHERE i.indrelid = t.relid AND a.attname = %s ORDER BY (am.amname = 'btree') DESC LIMIT 1) "
            "FROM pg_partition_tree(%s) t WHERE t.isleaf ORDER BY 1",
            (contract.time_column, oid),
        )
        leaves = cur.fetchall()
        f.partitions_without_time_index = [name for name, am in leaves if am is None]
        if leaves and not f.partitions_without_time_index:
            f.time_index = "(an index on every partition)"
            f.time_btree = all(am == "btree" for _, am in leaves)
    f.unique_missing_time = unique_indexes_missing_time(cur, contract)
    cur.execute(
        "SELECT conrelid::regclass::text || ' (' || conname || ')' FROM pg_constraint "
        "WHERE contype = 'f' AND confrelid = %s ORDER BY 1",
        (oid,),
    )
    f.referencing_fks = [r[0] for r in cur.fetchall()]
    # The table itself, and every partition of a partitioned parent
    # (pg_partition_tree yields nothing for a plain table).
    cur.execute(
        "SELECT t.tgrelid::regclass::text || '.' || t.tgname FROM pg_trigger t "
        "WHERE NOT t.tgisinternal AND (t.tgrelid = %s OR t.tgrelid IN "
        "(SELECT relid FROM pg_partition_tree(%s))) ORDER BY 1",
        (oid, oid),
    )
    f.triggers = [r[0] for r in cur.fetchall()]
    from ..conformance import dependent_views

    f.dependent_views = dependent_views(cur, contract.qualified)
    cur.execute(
        "SELECT DISTINCT n.nspname || '.' || c.relname FROM pg_depend d "
        "JOIN pg_rewrite r ON r.oid = d.objid JOIN pg_class c ON c.oid = r.ev_class "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE d.refobjid = %s AND c.relkind = 'm'",
        (oid,),
    )
    f.materialized_views = [r[0] for r in cur.fetchall()]
    return f


def time_range(conn, contract: TimeseriesContract, facts: SourceFacts) -> tuple[datetime | None, datetime | None, float]:
    """``(min, max, seconds it took)``. Instant with a btree on time; a single
    sequential pass otherwise, which the caller reports."""
    cur = conn.cursor()
    t0 = time.monotonic()
    cur.execute("SET LOCAL statement_timeout = 0") if not conn.autocommit else None
    cur.execute(f'SELECT min("{contract.time_column}"), max("{contract.time_column}") FROM {_q(contract.schema, contract.table)}')
    lo, hi = cur.fetchone()
    return _aware(lo), _aware(hi), time.monotonic() - t0


# -- estimate -----------------------------------------------------------------


@dataclass
class Estimate:
    qualified: str
    feasible: bool
    rows: int = 0
    bytes: int = 0
    range_lo: datetime | None = None
    range_hi: datetime | None = None
    range_seconds: float = 0.0
    rows_per_day: int | None = None
    window: timedelta | None = None
    windows: int = 0
    sample_rows: int = 0
    ratio: float | None = None
    expected_bytes: int | None = None
    needed_bytes: int | None = None
    free_bytes: int | None = None
    rows_per_second: float | None = None
    eta_seconds: float | None = None
    dependent_views: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["window"] = int(self.window.total_seconds()) if self.window else None
        for k in ("range_lo", "range_hi"):
            d[k] = getattr(self, k).isoformat() if getattr(self, k) else None
        return d


def _window_for(contract: TimeseriesContract, rows_per_day: int | None, window: timedelta | None) -> timedelta:
    if window is not None:
        return window
    if contract.chunk_interval is not None or contract.rows_per_day is not None:
        return contract.chunk
    from .contract import derive_chunk_interval

    return derive_chunk_interval(rows_per_day)


def _volume_free(conn, disk_path: str | None) -> int | None:
    import os

    candidates = [disk_path, os.environ.get("AXIOM_DB_DATA_PATH", "").strip()]
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
            return shutil.disk_usage(path).free
        except OSError:
            continue
    return None


def estimate(
    conn,
    contract: TimeseriesContract,
    *,
    window: timedelta | None = None,
    sample_rows: int = DEFAULT_SAMPLE_ROWS,
    disk_path: str | None = None,
) -> Estimate:
    """What the adoption would do, measured where it can be. Writes nothing
    that outlives the call (the sample hypertable is dropped)."""
    f = source_facts(conn, contract)
    est = Estimate(qualified=contract.qualified, feasible=True, rows=f.rows, bytes=f.bytes,
                   dependent_views=f.dependent_views)
    if not f.exists:
        est.blockers.append(f"{contract.qualified} does not exist")
    elif f.is_hypertable:
        est.blockers.append(f"{contract.qualified} is already a hypertable; apply the contract instead")
    elif partition_refusal(f, contract):
        est.blockers.append(partition_refusal(f, contract))
    else:
        if f.relkind == "p":
            est.notes.append(
                f"{contract.qualified} is a natively partitioned parent with {f.partitions} partition(s); "
                "it is read through the parent and the parent is what swaps out"
            )
        if f.time_index is None and f.relkind == "p" and f.partitions_without_time_index:
            est.blockers.append(
                f"no index leads with {contract.time_column} on partition(s) {', '.join(f.partitions_without_time_index)}, "
                "so every window would scan them whole. Build one online on each first (CREATE INDEX CONCURRENTLY "
                f"is not available on a partitioned parent): CREATE INDEX CONCURRENTLY ON <partition> "
                f"USING brin ({contract.time_column})"
            )
        elif f.time_index is None:
            est.blockers.append(
                f"no index leads with {contract.time_column}, so every window would scan the whole table. "
                f"Build one online first: CREATE INDEX CONCURRENTLY {contract.table}_{contract.time_column}_brin "
                f"ON {contract.qualified} USING brin ({contract.time_column})"
            )
        for name in f.unique_missing_time:
            est.blockers.append(
                f"unique index {name} omits {contract.time_column}; TimescaleDB requires it in every unique "
                "index, so the key must gain the time column before adoption"
            )
        for name in f.referencing_fks:
            est.blockers.append(
                f"foreign key {name} references {contract.qualified} and would follow the old table through "
                "the rename; drop it before adoption and recreate it after"
            )
        for name in f.triggers:
            est.blockers.append(f"trigger {name} is not carried to the new table; recreate it after the swap")
        for name in f.materialized_views:
            est.blockers.append(f"materialized view {name} cannot be rebound; drop and recreate it after the swap")
    if est.blockers:
        est.feasible = False
        return est

    lo, hi, secs = time_range(conn, contract, f)
    est.range_lo, est.range_hi, est.range_seconds = lo, hi, secs
    if not f.time_btree:
        est.notes.append(f"time range read with one sequential pass ({secs:.0f} s): no btree leads with {contract.time_column}")
    if lo is None or hi is None:
        est.notes.append("the table is empty: adopt it by applying the contract")
        est.windows = 0
        return est
    days = max((hi - lo).total_seconds() / 86400, 1 / 24)
    est.rows_per_day = int(f.rows / days) if f.rows else None
    est.window = _window_for(contract, est.rows_per_day, window)
    est.windows = len(plan_windows(lo, hi, est.window))

    # The ratio and the rate, measured on a sample copied the way the copy
    # will copy, into a scratch hypertable with the contract's compression.
    sample = _q(contract.schema, contract.table + SAMPLE_SUFFIX)
    cur = conn.cursor()
    cur.execute(f"DROP TABLE IF EXISTS {sample}")
    try:
        cur.execute(f"CREATE TABLE {sample} (LIKE {_q(contract.schema, contract.table)} INCLUDING ALL)")
        from dataclasses import replace

        apply(conn, replace(contract, table=contract.table + SAMPLE_SUFFIX, retain=None, forward_guard=False))
        # The newest full window: representative of what arrives, cheap to
        # reach through the time index.
        w_hi = align_down(hi, est.window)
        w_lo = w_hi - est.window
        if w_lo < lo:
            w_lo, w_hi = align_down(lo, est.window), align_down(lo, est.window) + est.window
        t0 = time.monotonic()
        cur.execute(
            f'INSERT INTO {sample} SELECT * FROM {_q(contract.schema, contract.table)} '
            f'WHERE "{contract.time_column}" >= %s AND "{contract.time_column}" < %s LIMIT %s',
            (w_lo, w_hi, int(sample_rows)),
        )
        copied = cur.rowcount
        elapsed = max(time.monotonic() - t0, 1e-3)
        est.sample_rows = int(copied)
        if copied:
            cur.execute(f"SELECT compress_chunk(c, if_not_compressed => true) FROM show_chunks('{sample}') c")
            cur.execute(
                "SELECT sum(before_compression_total_bytes), sum(after_compression_total_bytes) "
                f"FROM chunk_compression_stats('{sample}')"
            )
            before, after = cur.fetchone()
            if before and after:
                est.ratio = float(before) / float(after)
                est.expected_bytes = int(f.bytes / est.ratio)
            est.rows_per_second = copied / elapsed
            remaining = max(f.rows - copied, 0)
            # The sample does not pay for the hash of each window (both
            # sides); budget it as the same cost again.
            est.eta_seconds = 2 * remaining / est.rows_per_second
    finally:
        cur.execute(f"DROP TABLE IF EXISTS {sample}")

    est.free_bytes = _volume_free(conn, disk_path)
    if est.expected_bytes is not None:
        uncompressed_window = int(f.bytes * (est.window.total_seconds() / 86400) / days)
        est.needed_bytes = int(est.expected_bytes * FREE_SPACE_FACTOR) + uncompressed_window
        if est.free_bytes is not None and est.free_bytes < est.needed_bytes:
            est.blockers.append(
                f"{est.free_bytes / 2**30:.1f} GiB free on the volume, {est.needed_bytes / 2**30:.1f} GiB needed "
                "(the expected compressed size with headroom, plus one uncompressed window)"
            )
            est.feasible = False
    elif est.free_bytes is None:
        est.notes.append("free space not checked: the database volume is not visible from here (set AXIOM_DB_DATA_PATH or pass disk_path)")
    return est


# -- the copy -----------------------------------------------------------------


@dataclass
class Progress:
    qualified: str
    phase: str = "none"
    window_seconds: int = 0
    windows_total: int = 0
    windows_done: int = 0
    rows_estimate: int = 0
    rows_copied: int = 0
    rows_per_second: float | None = None
    eta_seconds: float | None = None
    range_lo: datetime | None = None
    boundary: datetime | None = None
    last_window_end: datetime | None = None
    started_at: datetime | None = None
    updated_at: datetime | None = None
    swapped_at: datetime | None = None
    verified_at: datetime | None = None
    failed_window: tuple[datetime, datetime] | None = None
    failure: str | None = None

    @property
    def percent(self) -> float:
        if self.phase in ("swapped", "verified", "dropped"):
            return 100.0
        if not self.windows_total:
            return 0.0
        return 100.0 * self.windows_done / self.windows_total

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["percent"] = round(self.percent, 2)
        for k in ("range_lo", "boundary", "last_window_end", "started_at", "updated_at", "swapped_at", "verified_at"):
            d[k] = getattr(self, k).isoformat() if getattr(self, k) else None
        d["failed_window"] = [w.isoformat() for w in self.failed_window] if self.failed_window else None
        return d


def _digest(cur, qualified_quoted: str, time_column: str, lo: datetime, hi: datetime) -> tuple[int, int]:
    """``(rows, order-independent hash)`` of a window. The hash is the sum of a
    64-bit prefix of each row's digest: commutative, so row order is
    irrelevant, and a repeated row counts twice, so a duplicate shows."""
    cur.execute(
        f"SELECT count(*), coalesce(sum(('x' || substr(md5(t::text), 1, 16))::bit(64)::bigint), 0) "
        f'FROM {qualified_quoted} t WHERE "{time_column}" >= %s AND "{time_column}" < %s',
        (lo, hi),
    )
    rows, digest = cur.fetchone()
    return int(rows), int(digest)


def _read_adoption(cur, qualified: str) -> dict | None:
    cur.execute(
        "SELECT phase, window_seconds, range_lo, range_hi, boundary, rows_estimate, tail_from, "
        f"started_at, updated_at, swapped_at, verified_at, notes FROM {ADOPTIONS} WHERE qualified = %s",
        (qualified,),
    )
    row = cur.fetchone()
    if row is None:
        return None
    keys = ("phase", "window_seconds", "range_lo", "range_hi", "boundary", "rows_estimate", "tail_from",
            "started_at", "updated_at", "swapped_at", "verified_at", "notes")
    return dict(zip(keys, row, strict=True))


def progress(conn, contract: TimeseriesContract) -> Progress:
    """What the ledger says, for a status line or the node's health."""
    _ensure_ledger(conn)
    cur = conn.cursor()
    p = Progress(qualified=contract.qualified)
    a = _read_adoption(cur, contract.qualified)
    if a is None:
        return p
    p.phase = a["phase"]
    p.window_seconds = int(a["window_seconds"])
    p.rows_estimate = int(a["rows_estimate"] or 0)
    p.range_lo, p.boundary = _aware(a["range_lo"]), _aware(a["boundary"])
    p.started_at, p.updated_at = _aware(a["started_at"]), _aware(a["updated_at"])
    p.swapped_at, p.verified_at = _aware(a["swapped_at"]), _aware(a["verified_at"])
    window = timedelta(seconds=p.window_seconds)
    if p.range_lo and p.boundary:
        p.windows_total = len(plan_windows(p.range_lo, p.boundary, window))
    cur.execute(
        f"SELECT count(*), coalesce(sum(rows), 0), max(window_end) FROM {WINDOWS} WHERE qualified = %s",
        (contract.qualified,),
    )
    done, copied, last = cur.fetchone()
    p.windows_done, p.rows_copied, p.last_window_end = int(done), int(copied), _aware(last)
    cur.execute(
        f"SELECT sum(rows), sum(seconds) FROM (SELECT rows, seconds FROM {WINDOWS} WHERE qualified = %s "
        "ORDER BY done_at DESC LIMIT 10) recent",
        (contract.qualified,),
    )
    rows, secs = cur.fetchone()
    if rows and secs and float(secs) > 0:
        p.rows_per_second = float(rows) / float(secs)
        remaining = max(p.rows_estimate - p.rows_copied, 0)
        p.eta_seconds = remaining / p.rows_per_second if p.phase == "copying" else 0.0
    return p


def _begin(conn, contract: TimeseriesContract, window: timedelta | None, tablespace: str | None) -> dict:
    """First run: create the copy from the contract and record the plan."""
    cur = conn.cursor()
    f = source_facts(conn, contract)
    est_window = window
    if est_window is None:
        lo, hi, _ = time_range(conn, contract, f)
        days = max(((hi - lo).total_seconds() / 86400) if lo and hi else 1, 1 / 24)
        est_window = _window_for(contract, int(f.rows / days) if f.rows else None, None)
    else:
        lo, hi, _ = time_range(conn, contract, f)
    new = contract.table + NEW_SUFFIX
    from dataclasses import replace

    new_contract = replace(contract, table=new, chunk_interval=est_window if contract.chunk_interval is None else contract.chunk_interval)
    if tablespace:
        cur.execute(f'SET default_tablespace = "{tablespace}"')
    cur.execute(f"CREATE TABLE {_q(contract.schema, new)} (LIKE {_q(contract.schema, contract.table)} INCLUDING ALL)")
    if tablespace:
        cur.execute("RESET default_tablespace")
    try:
        apply(conn, replace(new_contract, forward_guard=False))
        _carry_identity(cur, contract.schema, contract.table, new)
    except Exception:
        # Nothing is recorded yet: leave nothing half-made behind.
        cur.execute(f"DROP TABLE IF EXISTS {_q(contract.schema, new)}")
        raise
    boundary = None
    if lo is not None and hi is not None:
        now = datetime.now(UTC)
        boundary = min(align_up(hi + timedelta(microseconds=1), est_window), align_down(now, est_window))
        boundary = max(boundary, align_down(lo, est_window))
    cur.execute(
        f"INSERT INTO {ADOPTIONS} (qualified, phase, window_seconds, range_lo, range_hi, boundary, rows_estimate, notes) "
        "VALUES (%s, 'copying', %s, %s, %s, %s, %s, %s::jsonb) "
        "ON CONFLICT (qualified) DO UPDATE SET phase = 'copying', window_seconds = EXCLUDED.window_seconds, "
        "range_lo = EXCLUDED.range_lo, range_hi = EXCLUDED.range_hi, boundary = EXCLUDED.boundary, "
        "rows_estimate = EXCLUDED.rows_estimate, started_at = now(), updated_at = now(), "
        "swapped_at = NULL, verified_at = NULL, tail_from = NULL, notes = EXCLUDED.notes",
        (contract.qualified, int(est_window.total_seconds()), lo, hi, boundary, f.rows,
         _json({"tablespace": tablespace, "relkind": f.relkind, "dependent_views": f.dependent_views})),
    )
    return _read_adoption(cur, contract.qualified)


def _carry_identity(cur, schema: str, old: str, new: str) -> None:
    """The new table is owned by the old one's owner and carries its table
    comment (``LIKE ... INCLUDING ALL`` copies column, index and constraint
    comments, not the table's). Done before the copy so a role that may not do
    it fails the adoption at its start, not after hours."""
    from psycopg import sql

    cur.execute(
        "SELECT pg_get_userbyid(c.relowner), current_user::text, obj_description(c.oid, 'pg_class') "
        "FROM pg_class c WHERE c.oid = %s::regclass", (_q(schema, old),))
    owner, me, comment = cur.fetchone()
    target = sql.SQL("{}.{}").format(sql.Identifier(schema), sql.Identifier(new))
    if owner != me:
        try:
            cur.execute(sql.SQL("ALTER TABLE {} OWNER TO {}").format(target, sql.Identifier(owner)))
        except Exception as exc:
            raise AdoptionRefused(
                f"the new table cannot be given the old table's owner {owner} by {me}: {exc}. "
                f"Run as {owner} (or a superuser), so the adoption does not change who owns the data"
            ) from exc
    if comment:
        cur.execute(sql.SQL("COMMENT ON TABLE {} IS {}").format(target, sql.Literal(comment)))


def _json(obj: Any) -> str:
    import json

    return json.dumps(obj, default=str)


def run(
    conn,
    contract: TimeseriesContract,
    *,
    window: timedelta | None = None,
    max_windows: int | None = None,
    tablespace: str | None = None,
    lock_timeout: str = DEFAULT_LOCK_TIMEOUT,
    on_progress: Callable[[Progress], None] | None = None,
    _after_copy: Callable[..., None] | None = None,
) -> Progress:
    """Copy the windows not yet done, verifying each. Resumable. Stops at
    ``max_windows``, at the first window that will not verify, or when every
    window before the boundary is done.

    Single-flight per table across processes: a session advisory lock is held
    for the call, so a scheduled batch and an operator's ``--apply`` can never
    copy the same windows at once. The lock belongs to the connection, so a
    process that is killed releases it with its connection and the next call
    simply resumes. A second caller raises :class:`AdoptionBusy`.

    ``_after_copy(conn, contract, lo, hi, n)`` is a test seam that runs inside
    the window's transaction between the copy and its check, so the check can
    be shown to fail.
    """
    if not conn.autocommit:
        raise AdoptionRefused("the connection must be in autocommit mode")
    from axiom.infra.switchplan.lock import SingleFlight

    lock = SingleFlight(_lock_name(contract.qualified), conn=conn)
    if not lock.acquire():
        raise AdoptionBusy(f"{contract.qualified} is being copied by another process; nothing was started")
    try:
        return _run(conn, contract, window=window, max_windows=max_windows, tablespace=tablespace,
                    lock_timeout=lock_timeout, on_progress=on_progress, _after_copy=_after_copy)
    finally:
        lock.release()


def record_failure(conn, qualified: str, lo: datetime, hi: datetime, message: str) -> None:
    """Keep, in the ledger, that a window did not verify, so the node's health
    can say so after this process is gone. Outside the window's transaction (that
    one is rolled back); cleared when the window later verifies."""
    conn.execute(
        f"UPDATE {ADOPTIONS} SET notes = notes || jsonb_build_object('last_failure', "
        "jsonb_build_object('window_start', %s::text, 'window_end', %s::text, 'message', %s::text, "
        "'at', now()::text)) WHERE qualified = %s",
        (lo.isoformat(), hi.isoformat(), message, qualified),
    )


def _run(
    conn,
    contract: TimeseriesContract,
    *,
    window: timedelta | None,
    max_windows: int | None,
    tablespace: str | None,
    lock_timeout: str,
    on_progress: Callable[[Progress], None] | None,
    _after_copy: Callable[..., None] | None,
) -> Progress:
    import psycopg

    _ensure_ledger(conn)
    cur = conn.cursor()
    a = _read_adoption(cur, contract.qualified)
    if a is None or a["phase"] in ("rolled_back", "dropped") or a["phase"] == "copying":
        _refuse_partition(conn, contract)
    if a is None or a["phase"] in ("rolled_back", "dropped"):
        f = source_facts(conn, contract)
        if not f.exists:
            raise AdoptionRefused(f"{contract.qualified} does not exist")
        if f.is_hypertable:
            raise AdoptionRefused(f"{contract.qualified} is already a hypertable")
        est = estimate(conn, contract, window=window, sample_rows=1)
        if est.blockers:
            raise AdoptionRefused("; ".join(est.blockers))
        a = _begin(conn, contract, window, tablespace)
    elif a["phase"] != "copying":
        raise AdoptionRefused(f"{contract.qualified} is {a['phase']}; nothing to copy")

    src = _q(contract.schema, contract.table)
    new = _q(contract.schema, contract.table + NEW_SUFFIX)
    ts = contract.time_column
    wsecs = timedelta(seconds=int(a["window_seconds"]))
    p = progress(conn, contract)
    if a["range_lo"] is None or a["boundary"] is None:
        return p
    planned = plan_windows(_aware(a["range_lo"]), _aware(a["boundary"]), wsecs)
    cur.execute(f"SELECT window_start FROM {WINDOWS} WHERE qualified = %s", (contract.qualified,))
    done = {_aware(r[0]) for r in cur.fetchall()}
    todo = [(lo, hi) for lo, hi in planned if lo not in done]
    if max_windows is not None:
        todo = todo[: max(int(max_windows), 0)]

    for n, (lo, hi) in enumerate(todo):
        failure = None
        for attempt in range(WINDOW_ATTEMPTS):
            t0 = time.monotonic()
            try:
                with conn.transaction():
                    cur.execute(f"SET LOCAL lock_timeout = '{lock_timeout}'")
                    cur.execute(
                        f'INSERT INTO {new} SELECT * FROM {src} WHERE "{ts}" >= %s AND "{ts}" < %s', (lo, hi)
                    )
                    if _after_copy is not None:
                        _after_copy(conn, contract, lo, hi, n)
                    src_rows, src_digest = _digest(cur, src, ts, lo, hi)
                    new_rows, new_digest = _digest(cur, new, ts, lo, hi)
                    if src_rows != new_rows:
                        raise _Mismatch(f"row count differs: source {src_rows:,}, copy {new_rows:,}")
                    if src_digest != new_digest:
                        raise _Mismatch(f"row hash differs over {src_rows:,} rows")
                    cur.execute(
                        f"INSERT INTO {WINDOWS} (qualified, window_start, window_end, rows, digest, seconds) "
                        "VALUES (%s, %s, %s, %s, %s, %s)",
                        (contract.qualified, lo, hi, src_rows, src_digest, time.monotonic() - t0),
                    )
                    cur.execute(
                        f"UPDATE {ADOPTIONS} SET updated_at = now(), notes = notes - 'last_failure' "
                        "WHERE qualified = %s", (contract.qualified,))
                failure = None
                break
            except _Mismatch as exc:
                failure = str(exc)
                log.warning("adopt %s: window %s..%s attempt %d: %s", contract.qualified, lo, hi, attempt + 1, exc)
            except psycopg.errors.LockNotAvailable as exc:
                failure = f"lock not available within {lock_timeout}: {exc}"
                time.sleep(1)
        if failure is not None:
            try:
                record_failure(conn, contract.qualified, lo, hi, failure)
            except Exception:  # noqa: BLE001 - the failure is still returned to the caller
                log.warning("adopt %s: could not record the failed window in the ledger", contract.qualified)
            p = progress(conn, contract)
            p.failed_window, p.failure = (lo, hi), failure
            if on_progress:
                on_progress(p)
            return p
        # Compress what is now whole and verified, so the copy needs compressed
        # space. Outside the window's transaction: a compression that fails
        # leaves the window done and the policy to retry it.
        try:
            cur.execute(
                "SELECT compress_chunk(format('%%I.%%I', chunk_schema, chunk_name)::regclass, if_not_compressed => true) "
                "FROM timescaledb_information.chunks WHERE hypertable_schema = %s AND hypertable_name = %s "
                "AND range_end <= %s AND NOT is_compressed",
                (contract.schema, contract.table + NEW_SUFFIX, hi),
            )
        except Exception as exc:  # noqa: BLE001 - see above
            log.warning("adopt %s: compression deferred for chunks before %s: %s", contract.qualified, hi, exc)
        if on_progress:
            on_progress(progress(conn, contract))
    return progress(conn, contract)


class _Mismatch(Exception):
    pass


# -- the swap -----------------------------------------------------------------


def _views_and_grants(cur, qualified: str, contract: TimeseriesContract) -> tuple[list[tuple[str, str]], list]:
    from ..conformance import dependent_views, privileges_of

    views = []
    for view in dependent_views(cur, qualified):
        cur.execute("SELECT pg_get_viewdef(%s::regclass, true)", (view,))
        views.append((view, cur.fetchone()[0]))
    return views, privileges_of(cur, qualified)


def _rebind(cur, views: list[tuple[str, str]], target: str, privileges) -> None:
    from ..conformance import restore_privileges

    for view, definition in views:
        cur.execute(f"CREATE OR REPLACE VIEW {view} AS {definition}")
    restore_privileges(cur, target, privileges)


def _carry_sequences(cur, schema: str, old: str, new: str) -> list[str]:
    """Sequences the old table's columns own move to the new table (dropping
    the old would drop them, and every default that reads them), and an identity
    column's own sequence on the new table starts where the old one stands.
    Nothing here reads the table's rows: it runs under the swap's lock."""
    done: list[str] = []
    cur.execute(
        "SELECT s.oid::regclass::text, a.attname, d.deptype FROM pg_depend d "
        "JOIN pg_class s ON s.oid = d.objid AND s.relkind = 'S' "
        "JOIN pg_attribute a ON a.attrelid = d.refobjid AND a.attnum = d.refobjsubid "
        "WHERE d.refclassid = 'pg_class'::regclass AND d.refobjid = %s::regclass AND d.deptype IN ('a', 'i') "
        "ORDER BY 1",
        (f'"{schema}"."{old}"',),
    )
    for seq, col, deptype in cur.fetchall():
        target = f'"{schema}"."{new}"."{col}"'
        cur.execute("SELECT 1 FROM pg_attribute WHERE attrelid = %s::regclass AND attname = %s AND NOT attisdropped",
                    (f'"{schema}"."{new}"', col))
        if cur.fetchone() is None:
            continue
        if deptype == "a":  # serial: the column's default reads this sequence; it follows the column
            cur.execute(f"ALTER SEQUENCE {seq} OWNED BY {target}")
            done.append(f"sequence {seq} now owned by {schema}.{new}.{col}")
        else:  # identity: the new column has a sequence of its own, started past what the old handed out
            cur.execute("SELECT pg_get_serial_sequence(%s, %s)", (f'"{schema}"."{new}"', col))
            own = cur.fetchone()[0]
            if own:
                cur.execute(f"SELECT last_value, is_called FROM {seq}")
                last, called = cur.fetchone()
                cur.execute("SELECT setval(%s, %s, %s)", (own, last, called))
                done.append(f"sequence {own} set to {last} (as {seq})")
    return done


def _tree(cur, oid: int) -> set[int]:
    """The relation and everything that inherits from it (partitions, legacy
    children, a hypertable's chunks)."""
    cur.execute(
        "WITH RECURSIVE t(o) AS (SELECT %s::oid UNION ALL SELECT i.inhrelid FROM pg_inherits i JOIN t ON i.inhparent = t.o) "
        "SELECT o::int8 FROM t", (oid,))
    return {int(r[0]) for r in cur.fetchall()}


def _scanned(cur, name: str) -> set[int]:
    """The oids of the relations a ``SELECT *`` from ``name`` would scan, read
    from its plan (not run)."""
    import json

    cur.execute(f"EXPLAIN (FORMAT JSON, VERBOSE) SELECT * FROM {name}")  # VERBOSE names the schema
    plan = cur.fetchone()[0]
    if isinstance(plan, str):
        plan = json.loads(plan)
    found: list[tuple[str | None, str]] = []

    def walk(node: dict) -> None:
        if node.get("Relation Name"):
            found.append((node.get("Schema"), node["Relation Name"]))
        for child in node.get("Plans", ()):
            walk(child)

    walk(plan[0]["Plan"])
    out: set[int] = set()
    for schema, rel in found:
        qualified = f'"{schema}"."{rel}"' if schema else f'"{rel}"'
        cur.execute("SELECT to_regclass(%s)::oid::int8", (qualified,))
        row = cur.fetchone()
        if row is None or row[0] is None:
            # A relation the plan names that we cannot resolve is not "nothing":
            # fail the check rather than wave it through.
            raise SwapUnsafe(f"swap rolled back, nothing changed: the plan reads {qualified}, which cannot be identified")
        out.add(int(row[0]))
    return out


def check_reads_land_on_new(
    cur, *, qualified: str, old_oid: int, new_oid: int, parent: str | None = None, views: tuple[str, ...] = ()
) -> list[str]:
    """After the names have swapped (inside the swap's transaction): a read
    through the original name, through the parent it hung from if there is
    one, and through every rebound view must scan the NEW table and none of the
    OLD. Raises :class:`SwapUnsafe` otherwise, so the caller's transaction
    rolls the swap back. Returns the lines that say what was checked."""
    old_tree, new_tree = _tree(cur, old_oid), _tree(cur, new_oid)
    has_chunks = len(new_tree) > 1
    said: list[str] = []
    targets = [(qualified, True)] + ([(parent, True)] if parent else []) + [(v, False) for v in views]
    for name, must_hit_new in targets:
        scanned = _scanned(cur, name)
        stale = scanned & old_tree
        if stale or (must_hit_new and has_chunks and not scanned & new_tree):
            cur.execute("SELECT string_agg(oid::regclass::text, ', ') FROM unnest(%s::oid[]) oid", (sorted(scanned),))
            where = cur.fetchone()[0] or "nothing"
            raise SwapUnsafe(
                f"swap rolled back, nothing changed: after renaming, a read through {name} would scan {where}, "
                f"{'which is the old table' if stale else 'not the new table'}. "
                + (f"{name} is the parent the table hung from; a hypertable cannot be attached to it. " if name == parent else "")
                + "Readers would have stayed on the old table."
            )
        said.append(name)
    return [f"post-swap check passed: a read through {', '.join(said)} scans the new table, not the old"]


#: Rows a catch-up pass may still copy before the swap's lock is taken. A pass
#: under this is what "caught up" means; the figure is rows, not seconds,
#: because it is what the final pass under the lock will have to copy.
TAIL_THRESHOLD_ROWS = 20_000
#: Catch-up passes before the swap gives up (a writer that outpaces the copy).
MAX_CATCHUP_PASSES = 30
#: A pass starts this far before the newest time it saw, so a row that arrived
#: slightly late is found again (each pass copies only what the new table lacks).
TAIL_MARGIN = timedelta(seconds=30)


def catch_up_tail(
    conn,
    contract: TimeseriesContract,
    tail_from: datetime,
    *,
    threshold_rows: int = TAIL_THRESHOLD_ROWS,
    max_passes: int = MAX_CATCHUP_PASSES,
    margin: timedelta = TAIL_MARGIN,
) -> dict[str, Any]:
    """Copy what arrived since the copy's boundary **without any lock**, in
    passes, until a pass copies fewer than ``threshold_rows``. Each pass copies
    exactly the rows the new table lacks from ``from`` on (``EXCEPT ALL``, so a
    retried swap never duplicates), then moves ``from`` to just before the
    newest time it saw. Returns ``{"from": <where the final pass under the lock
    must start>, "rows": <copied outside the lock>, "passes": n, "last": <rows
    in the last pass>}``; raises :class:`AdoptionRefused` when the writer
    outpaces it, so the answer is a refusal and not a long lock."""
    cur = conn.cursor()
    schema, table, ts = contract.schema, contract.table, contract.time_column
    src, new = _q(schema, table), _q(schema, table + NEW_SUFFIX)
    cur.execute(f'SELECT 1 FROM {new} WHERE "{ts}" >= %s LIMIT 1', (tail_from,))
    fresh = cur.fetchone() is None
    from_, total, last = tail_from, 0, 0
    for n in range(1, max_passes + 1):
        cur.execute(f'SELECT max("{ts}") FROM {src}')
        newest = _aware(cur.fetchone()[0])
        if n == 1 and fresh:
            cur.execute(f'INSERT INTO {new} SELECT * FROM {src} WHERE "{ts}" >= %s', (from_,))
        else:
            cur.execute(
                f'INSERT INTO {new} SELECT * FROM {src} WHERE "{ts}" >= %s '
                f'EXCEPT ALL SELECT * FROM {new} WHERE "{ts}" >= %s', (from_, from_))
        last = cur.rowcount
        total += last
        if newest is not None:
            from_ = max(tail_from, newest - margin)
        if last < threshold_rows:
            return {"from": from_, "rows": total, "passes": n, "last": last}
    raise AdoptionRefused(
        f"{contract.qualified}: the tail is not catching up: after {max_passes} pass(es) the last still copied "
        f"{last:,} row(s) (threshold {threshold_rows:,}). The writer outpaces the copy; swapping now would copy "
        "that under the lock. Nothing was swapped; run --swap again when writes are slower, or raise the threshold"
    )


def swap_in_transaction(
    cur, contract: TimeseriesContract, *, tail_from: datetime, final_from: datetime, old_oid: int, new_oid: int,
    parent: str | None, after_rename: Callable[[Any, tuple[str, ...]], list[str]] | None = None,
) -> tuple[list[str], int]:
    """The swap proper, inside a transaction the caller owns and after its
    ACCESS EXCLUSIVE lock on the old table: the last few rows, the two renames,
    views, privileges, sequences, the check that readers land on the new table,
    the ledger. Returns ``(steps, rows copied under the lock)``. Raises
    :class:`SwapUnsafe` (the caller's transaction rolls back) if a read would
    still reach the old table."""
    schema, table, ts = contract.schema, contract.table, contract.time_column
    src, new = _q(schema, table), _q(schema, table + NEW_SUFFIX)
    cur.execute(
        f'INSERT INTO {new} SELECT * FROM {src} WHERE "{ts}" >= %s EXCEPT ALL SELECT * FROM {new} WHERE "{ts}" >= %s',
        (final_from, final_from))
    tail = cur.rowcount
    views, privileges = _views_and_grants(cur, contract.qualified, contract)
    cur.execute(f'ALTER TABLE {src} RENAME TO "{table + OLD_SUFFIX}"')
    cur.execute(f'ALTER TABLE {new} RENAME TO "{table}"')
    _rebind(cur, views, src, privileges)
    steps = _carry_sequences(cur, schema, table + OLD_SUFFIX, table)
    if views:
        steps.append(f"rebound {len(views)} dependent view(s): {', '.join(v for v, _ in views)}")
    view_names = tuple(v for v, _ in views)
    if after_rename is not None:
        # the switch runner's in-commit gate (ADR-197): the same check, declared as a probe
        steps.extend(after_rename(cur, view_names))
    else:
        steps.extend(check_reads_land_on_new(
            cur, qualified=contract.qualified, old_oid=old_oid, new_oid=new_oid, parent=parent, views=view_names))
    cur.execute(
        f"UPDATE {ADOPTIONS} SET phase = 'swapped', swapped_at = now(), updated_at = now(), tail_from = %s "
        "WHERE qualified = %s", (tail_from, contract.qualified))
    return steps, tail


def last_swap(conn, contract: TimeseriesContract) -> dict[str, Any]:
    """What the swap measured: lock time, rows copied outside and under it."""
    row = _read_adoption(conn.cursor(), contract.qualified)
    return dict((row or {}).get("notes", {}).get("swap") or {})


def swap(
    conn,
    contract: TimeseriesContract,
    *,
    lock_timeout: str = DEFAULT_LOCK_TIMEOUT,
    tail_threshold_rows: int = TAIL_THRESHOLD_ROWS,
    max_catchup_passes: int = MAX_CATCHUP_PASSES,
    principal: str | None = None,
) -> list[str]:
    """Catch the tail up outside any lock, then swap the names in one short
    transaction. The old table stays as ``<table>__old``. The exclusive lock
    covers only the last few rows and the renames; its measured hold time is
    reported and kept in the ledger.

    Runs on the switch runner (ADR-197): ``catch_up_tail`` is its ``prepare``,
    :func:`swap_in_transaction` its ``commit`` (inside a lock budget, with
    :func:`check_reads_land_on_new` as the in-commit probe), and a change record
    is written first. See :mod:`.adopt_switch`."""
    from . import adopt_switch

    return adopt_switch.swap_through_runner(
        conn, contract, lock_timeout=lock_timeout, tail_threshold_rows=tail_threshold_rows,
        max_catchup_passes=max_catchup_passes, principal=principal)


# -- verification, drop, rollback ---------------------------------------------


@dataclass
class Verification:
    ok: bool
    windows_checked: int = 0
    missing_rows: int = 0
    extra_rows: int = 0
    repaired_rows: int = 0
    failed_windows: list[tuple[datetime, datetime]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["failed_windows"] = [[a.isoformat(), b.isoformat()] for a, b in self.failed_windows]
        return d


def _unique_columns(cur, schema: str, table: str, time_column: str) -> list[str] | None:
    cur.execute(
        "SELECT array_agg(a.attname ORDER BY k.ord) FROM pg_index i "
        "JOIN pg_class c ON c.oid = i.indrelid JOIN pg_namespace n ON n.oid = c.relnamespace "
        "CROSS JOIN LATERAL unnest(i.indkey) WITH ORDINALITY k(attnum, ord) "
        "JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = k.attnum "
        "WHERE n.nspname = %s AND c.relname = %s AND i.indisunique AND i.indpred IS NULL "
        "GROUP BY i.indexrelid ORDER BY i.indisprimary DESC, count(*) LIMIT 1",
        (schema, table),
    )
    row = cur.fetchone()
    return list(row[0]) if row and row[0] else None


def verify(conn, contract: TimeseriesContract, *, repair: bool = False) -> Verification:
    """Every window of the old table against the new. A digest that differs is
    resolved exactly: rows the new table lacks are a loss (repaired from the
    old table with ``repair``); rows the new table has in addition were
    written since the swap and are fine."""
    _ensure_ledger(conn)
    cur = conn.cursor()
    a = _read_adoption(cur, contract.qualified)
    if a is None or a["phase"] not in ("swapped", "verified"):
        raise AdoptionRefused(f"{contract.qualified} is not swapped (phase {a['phase'] if a else 'none'})")
    schema, table, ts = contract.schema, contract.table, contract.time_column
    new, old = _q(schema, table), _q(schema, table + OLD_SUFFIX)
    window = timedelta(seconds=int(a["window_seconds"]))
    cur.execute(f'SELECT min("{ts}"), max("{ts}") FROM {old}')
    lo, hi = cur.fetchone()
    v = Verification(ok=True)
    if lo is None:
        v.notes.append("the old table is empty")
    else:
        key = _unique_columns(cur, schema, table, ts)
        for w_lo, w_hi in plan_windows(_aware(lo), _aware(hi) + timedelta(microseconds=1), window):
            v.windows_checked += 1
            o_rows, o_digest = _digest(cur, old, ts, w_lo, w_hi)
            n_rows, n_digest = _digest(cur, new, ts, w_lo, w_hi)
            if o_rows == n_rows and o_digest == n_digest:
                continue
            where = f'"{ts}" >= %s AND "{ts}" < %s'
            cur.execute(
                f"SELECT count(*) FROM (SELECT * FROM {old} WHERE {where} EXCEPT ALL SELECT * FROM {new} WHERE {where}) x",
                (w_lo, w_hi, w_lo, w_hi),
            )
            missing = int(cur.fetchone()[0])
            cur.execute(
                f"SELECT count(*) FROM (SELECT * FROM {new} WHERE {where} EXCEPT ALL SELECT * FROM {old} WHERE {where}) x",
                (w_lo, w_hi, w_lo, w_hi),
            )
            extra = int(cur.fetchone()[0])
            v.extra_rows += extra
            if missing == 0:
                continue
            if repair:
                with conn.transaction():
                    # A repair rewrites rows that may sit in compressed chunks,
                    # which TimescaleDB caps per transaction by default; the
                    # repair is the one place that cost is accepted.
                    cur.execute("SET LOCAL timescaledb.max_tuples_decompressed_per_dml_transaction = 0")
                    if key:
                        cols = ", ".join(f'"{c}"' for c in key)
                        cur.execute(
                            "SELECT string_agg(quote_ident(attname), ', ') FROM pg_attribute WHERE attrelid = %s::regclass "
                            "AND attnum > 0 AND NOT attisdropped", (f'"{schema}"."{table}"',))
                        all_cols = cur.fetchone()[0]
                        updates = ", ".join(f'"{c}" = EXCLUDED."{c}"' for c in all_cols.replace('"', "").split(", ") if c not in key)
                        cur.execute(
                            f"INSERT INTO {new} SELECT * FROM {old} WHERE {where} EXCEPT ALL SELECT * FROM {new} WHERE {where} "
                            f"ON CONFLICT ({cols}) DO UPDATE SET {updates}",
                            (w_lo, w_hi, w_lo, w_hi),
                        )
                    else:
                        cur.execute(
                            f"INSERT INTO {new} SELECT * FROM {old} WHERE {where} EXCEPT ALL SELECT * FROM {new} WHERE {where}",
                            (w_lo, w_hi, w_lo, w_hi),
                        )
                    v.repaired_rows += cur.rowcount
                cur.execute(
                    f"SELECT count(*) FROM (SELECT * FROM {old} WHERE {where} EXCEPT ALL SELECT * FROM {new} WHERE {where}) x",
                    (w_lo, w_hi, w_lo, w_hi),
                )
                if int(cur.fetchone()[0]) == 0:
                    continue
            v.ok = False
            v.missing_rows += missing
            v.failed_windows.append((w_lo, w_hi))
    if v.ok:
        cur.execute(f"UPDATE {ADOPTIONS} SET phase = 'verified', verified_at = now(), updated_at = now() WHERE qualified = %s", (contract.qualified,))
    else:
        cur.execute(f"UPDATE {ADOPTIONS} SET phase = 'swapped', verified_at = NULL, updated_at = now() WHERE qualified = %s", (contract.qualified,))
    return v


def drop_old(
    conn, contract: TimeseriesContract, *, lock_timeout: str = DEFAULT_LOCK_TIMEOUT, reverify: bool = True,
    principal: str | None = None,
) -> list[str]:
    """Drop the old table: the plan's ``retire`` (ADR-197 D6) when the swap ran on
    the runner, otherwise (a swap made before it existed) directly. Either way only
    after a verification, and re-verified unless ``reverify=False``."""
    from . import adopt_switch

    return adopt_switch.drop_through_runner(
        conn, contract, lock_timeout=lock_timeout, reverify=reverify, principal=principal)


def _drop_old_direct(
    conn, contract: TimeseriesContract, *, lock_timeout: str = DEFAULT_LOCK_TIMEOUT, reverify: bool = True
) -> list[str]:
    """Drop the old table (a partitioned parent goes with its partitions). Only
    after a verification passed since the swap, and, unless the caller has just
    run one itself (``reverify=False``), after verifying again now: rows can be
    lost from the new table between a verification and the drop, and the drop is
    the one step that cannot be undone."""
    _ensure_ledger(conn)
    cur = conn.cursor()
    a = _read_adoption(cur, contract.qualified)
    if a is None or a["phase"] != "verified" or a["verified_at"] is None:
        raise AdoptionRefused(
            f"{contract.qualified}: the old table is dropped only after a verification passes "
            f"(phase {a['phase'] if a else 'none'}); run verify first"
        )
    if reverify:
        v = verify(conn, contract)
        if not v.ok:
            raise AdoptionRefused(
                f"{contract.qualified}: re-verifying before the drop found {v.missing_rows:,} row(s) of the old table "
                f"missing from the new one in {len(v.failed_windows)} window(s); nothing was dropped. "
                "Run verify with repair, or roll back"
            )
    old = _q(contract.schema, contract.table + OLD_SUFFIX)
    cur.execute(f"SET lock_timeout = '{lock_timeout}'")
    cur.execute("SELECT %s::regclass::oid::int8", (f'"{contract.schema}"."{contract.table + OLD_SUFFIX}"',))
    tree = _tree(cur, int(cur.fetchone()[0]))
    cur.execute("SELECT coalesce(sum(pg_total_relation_size(o)), 0) FROM unnest(%s::oid[]) o", (sorted(tree),))
    freed = int(cur.fetchone()[0] or 0)
    cur.execute(f"DROP TABLE {old}")
    cur.execute(f"UPDATE {ADOPTIONS} SET phase = 'dropped', updated_at = now() WHERE qualified = %s", (contract.qualified,))
    parts = f" with its {len(tree) - 1} partition(s)" if len(tree) > 1 else ""
    return [f"{contract.schema}.{contract.table + OLD_SUFFIX} dropped{parts}: {freed / 2**30:.2f} GiB returned"]


def rollback(conn, contract: TimeseriesContract, *, lock_timeout: str = DEFAULT_LOCK_TIMEOUT) -> list[str]:
    """Undo (see :func:`_rollback_direct`). After a swap made on the runner the
    runner's ``rollback`` is what runs, so the undo is a recorded change."""
    from . import adopt_switch

    return adopt_switch.rollback_through_runner(conn, contract, lock_timeout=lock_timeout)


def _rollback_direct(conn, contract: TimeseriesContract, *, lock_timeout: str = DEFAULT_LOCK_TIMEOUT) -> list[str]:
    """Undo. Before the swap: the copy and its ledger go. After it: the names
    swap back, the views rebind, rows written to the hypertable since the swap
    are carried back, and the copy goes."""
    import psycopg

    _ensure_ledger(conn)
    cur = conn.cursor()
    a = _read_adoption(cur, contract.qualified)
    if a is None or a["phase"] in ("none", "rolled_back", "dropped"):
        raise AdoptionRefused(f"{contract.qualified}: nothing to roll back (phase {a['phase'] if a else 'none'})")
    schema, table, ts = contract.schema, contract.table, contract.time_column
    steps: list[str] = []
    if a["phase"] == "copying":
        cur.execute(f"DROP TABLE IF EXISTS {_q(schema, table + NEW_SUFFIX)}")
        steps.append(f"{schema}.{table + NEW_SUFFIX} dropped; {schema}.{table} was never touched")
    else:
        current, old = _q(schema, table), _q(schema, table + OLD_SUFFIX)
        last_error = None
        for attempt in range(SWAP_ATTEMPTS):
            try:
                with conn.transaction():
                    cur.execute(f"SET LOCAL lock_timeout = '{lock_timeout}'")
                    cur.execute(f"LOCK TABLE {current} IN ACCESS EXCLUSIVE MODE")
                    swapped_at = a["swapped_at"]
                    tail_from = a["tail_from"]
                    # Rows the writer put into the hypertable since the swap go
                    # back to the plain table, so nothing written is lost.
                    cur.execute(
                        f'INSERT INTO {old} SELECT * FROM {current} WHERE "{ts}" >= %s '
                        f"EXCEPT ALL SELECT * FROM {old} WHERE \"{ts}\" >= %s",
                        (tail_from, tail_from),
                    )
                    carried = cur.rowcount
                    views, privileges = _views_and_grants(cur, contract.qualified, contract)
                    cur.execute(f'ALTER TABLE {current} RENAME TO "{table + NEW_SUFFIX}"')
                    cur.execute(f'ALTER TABLE {old} RENAME TO "{table}"')
                    _rebind(cur, views, current, privileges)
                    steps.extend(_carry_sequences(cur, schema, table + NEW_SUFFIX, table))
                    cur.execute(f"DROP TABLE {_q(schema, table + NEW_SUFFIX)}")
                steps.insert(0, f"{contract.qualified} is the plain table again (swapped {swapped_at}); "
                                f"{carried:,} row(s) written since the swap carried back; the copy dropped")
                break
            except psycopg.errors.LockNotAvailable as exc:
                last_error = exc
                time.sleep(min(2 ** attempt, 30))
        else:
            raise AdoptionRefused(f"could not take the rollback lock in {SWAP_ATTEMPTS} attempts: {last_error}")
    cur.execute(f"DELETE FROM {WINDOWS} WHERE qualified = %s", (contract.qualified,))
    cur.execute(f"UPDATE {ADOPTIONS} SET phase = 'rolled_back', updated_at = now() WHERE qualified = %s", (contract.qualified,))
    return steps


def eta_text(seconds: float | None) -> str:
    if seconds is None:
        return "unknown"
    if seconds < 90:
        return f"{seconds:.0f} s"
    if seconds < 5400:
        return f"{seconds / 60:.0f} min"
    return f"{seconds / 3600:.1f} h"


__all__ = [
    "ADOPTIONS",
    "DEFAULT_SAMPLE_ROWS",
    "LEDGER_SCHEMA",
    "NEW_SUFFIX",
    "OLD_SUFFIX",
    "PHASES",
    "WINDOWS",
    "AdoptionRefused",
    "Estimate",
    "PartitionRefused",
    "SwapUnsafe",
    "Progress",
    "SourceFacts",
    "Verification",
    "align_down",
    "align_up",
    "catch_up_tail",
    "drop_old",
    "estimate",
    "eta_text",
    "plan_windows",
    "progress",
    "rollback",
    "run",
    "last_swap",
    "source_facts",
    "swap",
    "swap_in_transaction",
    "time_range",
    "verify",
]
