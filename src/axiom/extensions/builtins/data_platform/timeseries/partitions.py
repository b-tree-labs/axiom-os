# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Declarative range-partition lifecycle for a time-series table.

A `RANGE (ts)` partitioned table is only partitioned in the sense that it *can*
be. If nobody creates the time partitions, every row lands in `DEFAULT`, and the
table behaves exactly like an unpartitioned one while looking partitioned in the
schema. That is not hypothetical: on one production node a
telemetry table was declared `RANGE (ts)` with a single `DEFAULT` partition,
and **337 million rows, 174 GB of data and 181 GB of indexes** — 72%
of a 496 GB database — accumulated in the catch-all. Nothing failed. Nothing
warned. Retention was impossible, because dropping a month meant a mass `DELETE`
that bloats rather than a `DROP TABLE` that returns the space.

This module is the missing half: create the partitions ahead of the data,
report honestly on where rows actually are, and retire old partitions by
dropping them.

**The hard part is doing it online, and it is the reason this is a module
rather than three SQL statements.** Adding a partition to a parent whose
`DEFAULT` already holds rows makes Postgres scan `DEFAULT` to prove no row
belongs in the new range — under `ACCESS EXCLUSIVE`, which stops writers for as
long as the scan takes. On a 174 GB default partition that is an outage.
Postgres will *skip* that scan when a validated `CHECK` constraint on `DEFAULT`
already proves the rows cannot be there. So the safe order is:

1. `ADD CONSTRAINT … CHECK (ts < boundary) NOT VALID` — instant, no scan.
2. `VALIDATE CONSTRAINT` — a full scan, but under `SHARE UPDATE EXCLUSIVE`,
   so writers keep running.
3. Create partitions at or after `boundary` — scan skipped.

`boundary` must be in the future, so rows still arriving satisfy the constraint
while it is validated.

Nothing here is domain-specific: the parent table, the partition key, the
granularity and the retention window are all arguments. It knows about time
partitions, not about what is being measured.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

log = logging.getLogger(__name__)

GRANULARITIES = ("day", "week", "month")

#: How long any DDL statement here may wait for its lock. Short on purpose: a
#: maintenance job must never queue in front of the writers on a live table.
DEFAULT_LOCK_TIMEOUT = "5s"

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _ident(name: str, what: str) -> str:
    """Validate a SQL identifier. Identifiers cannot be parameterised, so the
    only safe options are validation or quoting; this does both."""
    if not _IDENT.match(name or ""):
        raise ValueError(f"{what} must be a plain SQL identifier, got {name!r}")
    return f'"{name}"'


@dataclass(frozen=True)
class PartitionSpec:
    """What the partition lifecycle for one table should look like."""

    schema: str
    table: str
    """The partitioned PARENT, not a partition."""
    granularity: str = "day"
    ahead: int = 7
    """How many future periods to keep created. The next partition existing
    BEFORE its data arrives is the whole point; a missing one silently routes
    rows back to DEFAULT."""
    retain_periods: int | None = None
    """Drop partitions older than this many periods. ``None`` never drops."""

    def __post_init__(self) -> None:
        _ident(self.schema, "schema")
        _ident(self.table, "table")
        if self.granularity not in GRANULARITIES:
            raise ValueError(
                f"granularity must be one of {GRANULARITIES}, got {self.granularity!r}"
            )
        if self.ahead < 1:
            raise ValueError(
                "ahead must be >= 1: a partition created only once its data arrives is too late"
            )
        if self.retain_periods is not None and self.retain_periods < 1:
            raise ValueError("retain_periods must be >= 1 when set")

    @property
    def qualified(self) -> str:
        return f"{_ident(self.schema, 'schema')}.{_ident(self.table, 'table')}"


def period_start(moment: datetime, granularity: str) -> datetime:
    """The start of the period *moment* falls in, in UTC."""
    m = moment.astimezone(UTC)
    day = m.replace(hour=0, minute=0, second=0, microsecond=0)
    if granularity == "day":
        return day
    if granularity == "week":  # ISO weeks start Monday
        return day - timedelta(days=day.weekday())
    if granularity == "month":
        return day.replace(day=1)
    raise ValueError(f"unknown granularity {granularity!r}")


def next_period(start: datetime, granularity: str) -> datetime:
    if granularity == "day":
        return start + timedelta(days=1)
    if granularity == "week":
        return start + timedelta(days=7)
    if granularity == "month":
        return (start.replace(day=28) + timedelta(days=4)).replace(day=1)
    raise ValueError(f"unknown granularity {granularity!r}")


def partition_name(table: str, start: datetime, granularity: str) -> str:
    """Deterministic, sortable, and readable in `\\dt`."""
    stamp = {"day": "%Y%m%d", "week": "w%G%V", "month": "%Y%m"}[granularity]
    return f"{table}_p{start.strftime(stamp)}"


@dataclass(frozen=True)
class PlannedPartition:
    name: str
    start: datetime
    end: datetime

    def create_sql(self, spec: PartitionSpec) -> str:
        return (
            f"CREATE TABLE IF NOT EXISTS {_ident(spec.schema, 'schema')}.{_ident(self.name, 'partition')} "
            f"PARTITION OF {spec.qualified} "
            f"FOR VALUES FROM ('{self.start.isoformat()}') TO ('{self.end.isoformat()}')"
        )


def plan_partitions(
    now: datetime, spec: PartitionSpec, existing: set[str]
) -> list[PlannedPartition]:
    """The partitions that should exist and do not, covering the CURRENT period
    through ``ahead`` future ones.

    Pure: no database. The current period is included deliberately — a table
    partitioned today needs somewhere for today's rows to go.
    """
    out: list[PlannedPartition] = []
    start = period_start(now, spec.granularity)
    for _ in range(spec.ahead + 1):
        end = next_period(start, spec.granularity)
        name = partition_name(spec.table, start, spec.granularity)
        if name not in existing:
            out.append(PlannedPartition(name=name, start=start, end=end))
        start = end
    return out


def plan_retirements(
    now: datetime, spec: PartitionSpec, partitions: list[tuple[str, datetime, datetime]]
) -> list[str]:
    """Partitions lying ENTIRELY before the retention window.

    Pure. A partition whose range overlaps the cutoff at all is kept: dropping
    it would discard rows inside the window, and a retention policy that
    silently deletes retained data is worse than none.
    """
    if spec.retain_periods is None:
        return []
    cutoff = period_start(now, spec.granularity)
    for _ in range(spec.retain_periods):
        cutoff = _prev_period(cutoff, spec.granularity)
    return [name for name, _lo, hi in partitions if hi <= cutoff]


def _prev_period(start: datetime, granularity: str) -> datetime:
    if granularity == "day":
        return start - timedelta(days=1)
    if granularity == "week":
        return start - timedelta(days=7)
    if granularity == "month":
        return (start - timedelta(days=1)).replace(day=1)
    raise ValueError(f"unknown granularity {granularity!r}")


@dataclass
class PartitionReport:
    """Where the rows actually are, which is not what the schema implies."""

    parent: str
    partition_key: str = ""
    partitions: list[tuple[str, str, int]] = field(default_factory=list)
    """(name, bound expression, estimated rows) — estimates, never a count(*)
    on a table this size."""
    default_partition: str | None = None
    default_rows: int = 0
    default_bytes: int = 0
    missing: list[str] = field(default_factory=list)
    retirable: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def default_occupied(self) -> bool:
        """Rows in DEFAULT mean partition routing is not covering live data —
        the condition that produced a 337M-row catch-all."""
        return self.default_rows > 0

    def as_dict(self) -> dict:
        return {
            "parent": self.parent,
            "partition_key": self.partition_key,
            "partitions": [{"name": n, "bounds": b, "rows": r} for n, b, r in self.partitions],
            "default_partition": self.default_partition,
            "default_rows": self.default_rows,
            "default_bytes": self.default_bytes,
            "default_occupied": self.default_occupied,
            "missing": list(self.missing),
            "retirable": list(self.retirable),
            "notes": list(self.notes),
        }


def report(cur, spec: PartitionSpec, *, now: datetime | None = None) -> PartitionReport:
    """Read the parent's partitions and say where rows are.

    Row counts come from ``pg_class.reltuples`` (the planner's estimate), never
    ``count(*)``: counting 337M rows to produce a health report is how a health
    report becomes the thing that times out.
    """
    now = now or datetime.now(UTC)
    cur.execute(
        "SELECT pg_get_partkeydef(c.oid) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = %s AND c.relname = %s AND c.relkind = 'p'",
        (spec.schema, spec.table),
    )
    row = cur.fetchone()
    rep = PartitionReport(parent=f"{spec.schema}.{spec.table}")
    if not row:
        rep.notes.append(f"{rep.parent} is not a partitioned table")
        return rep
    rep.partition_key = row[0]
    cur.execute(
        "SELECT c.relname, pg_get_expr(c.relpartbound, c.oid), c.reltuples::bigint, "
        "       pg_total_relation_size(c.oid) "
        "FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid "
        "JOIN pg_class p ON p.oid = i.inhparent JOIN pg_namespace n ON n.oid = p.relnamespace "
        "WHERE n.nspname = %s AND p.relname = %s ORDER BY c.relname",
        (spec.schema, spec.table),
    )
    existing: set[str] = set()
    for name, bounds, tuples, total in cur.fetchall():
        if (bounds or "").strip().upper() == "DEFAULT":
            rep.default_partition = name
            rep.default_rows = max(0, int(tuples or 0))
            rep.default_bytes = int(total or 0)
        else:
            existing.add(name)
            rep.partitions.append((name, bounds, max(0, int(tuples or 0))))
    rep.missing = [p.name for p in plan_partitions(now, spec, existing)]
    rep.retirable = plan_retirements(now, spec, _bounds_of(rep.partitions))
    if rep.default_occupied:
        rep.notes.append(
            f"{rep.default_rows:,} estimated rows sit in {rep.default_partition}: partition "
            "coverage does not include live data, so retention cannot drop them"
        )
    if rep.missing:
        rep.notes.append(
            f"missing {len(rep.missing)} partition(s); rows in those periods fall to DEFAULT"
        )
    return rep


_BOUND = re.compile(r"FOR VALUES FROM \('([^']+)'\) TO \('([^']+)'\)")


def _bounds_of(partitions: list[tuple[str, str, int]]) -> list[tuple[str, datetime, datetime]]:
    out = []
    for name, bounds, _rows in partitions:
        m = _BOUND.search(bounds or "")
        if not m:
            continue
        try:
            lo = datetime.fromisoformat(m.group(1))
            hi = datetime.fromisoformat(m.group(2))
        except ValueError:
            continue
        out.append((name, _aware(lo), _aware(hi)))
    return out


def _aware(dt: datetime) -> datetime:
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def default_guard_sql(
    spec: PartitionSpec,
    default_partition: str,
    boundary: datetime,
    key: str,
    *,
    now: datetime | None = None,
) -> tuple[str, str, str]:
    """The three statements that let partitions be added WITHOUT an
    ACCESS EXCLUSIVE scan of a populated DEFAULT.

    Returns (add_not_valid, validate, constraint_name). The caller runs them as
    separate transactions: `VALIDATE` takes SHARE UPDATE EXCLUSIVE and can run
    for a long time on a large table, and holding it inside a transaction with
    other DDL would defeat the point.

    ``now`` is injectable, like every other clock in this module
    (:func:`plan_partitions`, :func:`plan_retirements`, :func:`report`). This
    function alone read the wall clock, which made "is the boundary in the
    future?" a question about the day the test ran rather than about the
    arguments: a caller that froze time got the frozen answer everywhere else
    and the real one here. A test written against a fixed NOW passed for two
    days and then failed on the third for no change in the code.
    """
    if not default_partition:
        raise ValueError("no default partition to guard")
    if boundary <= (now or datetime.now(UTC)):
        # The constraint must hold for rows still arriving while it validates.
        raise ValueError(
            "boundary must be in the future: rows are still being written into DEFAULT"
        )
    part = _ident(default_partition, "default partition")
    con = f"{spec.table}_default_upper"
    _ident(con, "constraint")
    _ident(key, "partition key column")
    qual = f"{_ident(spec.schema, 'schema')}.{part}"
    return (
        f"ALTER TABLE {qual} ADD CONSTRAINT {_ident(con, 'constraint')} "
        f"CHECK ({_ident(key, 'key')} < '{boundary.isoformat()}') NOT VALID",
        f"ALTER TABLE {qual} VALIDATE CONSTRAINT {_ident(con, 'constraint')}",
        con,
    )


__all__ = [
    "DEFAULT_LOCK_TIMEOUT",
    "GRANULARITIES",
    "PartitionReport",
    "PartitionSpec",
    "PlannedPartition",
    "default_guard_sql",
    "next_period",
    "partition_name",
    "period_start",
    "plan_partitions",
    "plan_retirements",
    "report",
]
