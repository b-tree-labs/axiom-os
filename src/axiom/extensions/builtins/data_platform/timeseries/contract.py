# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A table declared as time series gets its storage shape from one declaration.

Declared once, applied everywhere. A :class:`TimeseriesContract` says which
table, which column is time, which columns are its identity, and how fast it
grows. From that the platform derives and installs, idempotently:

- a TimescaleDB hypertable with a **chunk interval derived from the rate**
  (about :data:`TARGET_ROWS_PER_CHUNK` rows per chunk, snapped to a ladder of
  sensible widths);
- **compression** segmented by the identity columns and ordered by time
  descending, with a policy compressing chunks older than ``compress_after``
  (default seven days);
- an **optional retention** policy that is never a default: deleting a site's
  history is never implicit. A ``forward_guard`` on a table that is this
  node's forwarded copy keeps the retention job paused until a pass confirms
  delivery upstream (the #1250 rule: the forwarder's cursor is the boundary).

Why a declaration and not a heuristic. On the node this was written for, a
466 GB plain table (860 million rows, seven repeated low-cardinality text
columns, a timestamp and a double) sat beside an installed, idle TimescaleDB
for a year. A probe on one real hour measured **113.9x** compression with
exactly this shape of contract (segment by the identity columns, order by
time descending). Nothing applied it, because nothing had been told to.

**It reports the difference, not the outcome.** :func:`apply` returns the
steps it actually took, read back from the catalogue, so a deploy log says
``silver.readings: compress chunks older than 2 days (was 7 days)`` or says
nothing changed, and both are checkable.

**What it refuses.** Converting a populated plain table in place
(``migrate_data``) locks it for the length of a full copy, so above
:data:`IN_PLACE_ROW_LIMIT` rows or :data:`IN_PLACE_BYTES_LIMIT` bytes the
contract refuses and names ``axi data adopt-timeseries``, the lock-light,
resumable path. TimescaleDB requires the time column in every unique index;
a table whose key omits it is refused with the index named.

Nothing here is domain-specific: the table, the time column, the identity
columns and the rate are all declared. It knows about time-series storage,
not about what is being measured.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

log = logging.getLogger(__name__)

#: Compress a chunk once its newest row is this old. A week keeps the window
#: a late correction or a re-ingest is likely to touch uncompressed.
DEFAULT_COMPRESS_AFTER = timedelta(days=7)

#: The chunk width when no rate is declared.
DEFAULT_CHUNK_INTERVAL = timedelta(days=1)

#: What a chunk should hold. Ten million rows of a typical reading (about a
#: hundred bytes each, plus indexes) is on the order of a gigabyte, which keeps
#: a chunk and its indexes inside the buffer pool of a modest server.
TARGET_ROWS_PER_CHUNK = 10_000_000

#: Chunk widths a derived interval snaps to. Never narrower than an hour (chunk
#: overhead dominates) and never wider than a month (one chunk should never be
#: a year of data retention cannot drop in parts).
CHUNK_LADDER: tuple[timedelta, ...] = (
    timedelta(hours=1), timedelta(hours=2), timedelta(hours=4), timedelta(hours=6),
    timedelta(hours=12), timedelta(days=1), timedelta(days=7), timedelta(days=30),
)

#: Above either of these a plain table is not converted in place.
IN_PLACE_ROW_LIMIT = 1_000_000
IN_PLACE_BYTES_LIMIT = 1024**3

#: How long any DDL statement here may wait for its lock. Short on purpose: a
#: deploy must never queue in front of writers on a live table.
DEFAULT_LOCK_TIMEOUT = "5s"

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_LOCK_TIMEOUT = re.compile(r"^\d+(ms|s|min|h)?$")
_DURATION = re.compile(r"^\s*(\d+)\s*([a-zA-Z]*)\s*$")
_UNITS = {
    "": "days", "d": "days", "day": "days", "days": "days",
    "h": "hours", "hr": "hours", "hour": "hours", "hours": "hours",
    "m": "minutes", "min": "minutes", "minute": "minutes", "minutes": "minutes",
    "s": "seconds", "sec": "seconds", "second": "seconds", "seconds": "seconds",
    "w": "weeks", "week": "weeks", "weeks": "weeks",
}


class ContractRefused(Exception):
    """The declaration cannot be applied safely as it stands; the message says
    what to change. Never raised for a transient condition."""


def _ident(name: Any, what: str) -> str:
    if not isinstance(name, str) or not _IDENT.match(name):
        raise ValueError(f"{what} must be a plain SQL identifier, got {name!r}")
    return name


def parse_duration(value: Any) -> timedelta:
    """``7d``, ``12h``, ``30m``, ``90 days``, ``2w``; a bare number is days.

    Zero and negative are refused: a policy of "compress after no time" or
    "keep for no days" is a typo, not a decision.
    """
    if isinstance(value, timedelta):
        out = value
    elif isinstance(value, bool) or value is None:
        raise ValueError(f"not a duration: {value!r}")
    elif isinstance(value, (int, float)):
        out = timedelta(days=float(value))
    else:
        m = _DURATION.match(str(value))
        if not m or m.group(2).lower() not in _UNITS:
            raise ValueError(f"not a duration: {value!r} (use e.g. 7d, 12h, 30m, 90 days)")
        out = timedelta(**{_UNITS[m.group(2).lower()]: int(m.group(1))})
    if out <= timedelta(0):
        raise ValueError(f"a duration must be positive, got {value!r}")
    return out


def derive_chunk_interval(rows_per_day: int | None) -> timedelta:
    """The widest rung of :data:`CHUNK_LADDER` holding at most
    :data:`TARGET_ROWS_PER_CHUNK` rows at the declared rate; the default when
    no rate is declared; the floor when even an hour exceeds the target."""
    if not rows_per_day or rows_per_day <= 0:
        return DEFAULT_CHUNK_INTERVAL
    fitting = [c for c in CHUNK_LADDER if rows_per_day * c.total_seconds() / 86400 <= TARGET_ROWS_PER_CHUNK]
    return max(fitting) if fitting else CHUNK_LADDER[0]


def _interval_sql(td: timedelta) -> str:
    """A timedelta as a Postgres interval literal, whole seconds."""
    return f"{int(td.total_seconds())} seconds"


def _human(td: timedelta) -> str:
    secs = int(td.total_seconds())
    if secs % 86400 == 0:
        n = secs // 86400
        return f"{n} day{'s' if n != 1 else ''}"
    if secs % 3600 == 0:
        n = secs // 3600
        return f"{n} hour{'s' if n != 1 else ''}"
    if secs % 60 == 0:
        n = secs // 60
        return f"{n} minute{'s' if n != 1 else ''}"
    return f"{secs} seconds"


@dataclass(frozen=True)
class TimeseriesContract:
    """What one time-series table's storage should look like."""

    schema: str
    table: str
    time_column: str = "ts"
    #: The columns that identify a series. Compression segments by these, so a
    #: compressed chunk holds one run of values per series, which is where the
    #: measured 113.9x comes from.
    identity: tuple[str, ...] = ()
    #: The declared rate. The chunk interval derives from it.
    rows_per_day: int | None = None
    #: An explicit width overrides the derivation.
    chunk_interval: timedelta | None = None
    compress_after: timedelta = DEFAULT_COMPRESS_AFTER
    #: ``None`` keeps everything, which is the default.
    retain: timedelta | None = None
    #: This table is the copy this node forwards upstream: its retention runs
    #: only while no batch awaits delivery (the #1250 rule).
    forward_guard: bool = False

    def __post_init__(self) -> None:
        _ident(self.schema, "schema")
        _ident(self.table, "table")
        _ident(self.time_column, "time_column")
        object.__setattr__(self, "identity", tuple(_ident(c, "identity column") for c in self.identity))
        if self.time_column in self.identity:
            raise ValueError("the time column is not an identity column")
        if self.rows_per_day is not None and self.rows_per_day < 0:
            raise ValueError("rows_per_day must not be negative")
        for name in ("chunk_interval", "compress_after", "retain"):
            value = getattr(self, name)
            if value is not None and value <= timedelta(0):
                raise ValueError(f"{name} must be positive")

    @classmethod
    def from_entry(cls, entry: dict[str, Any]) -> TimeseriesContract:
        """Parse one ``[[data_platform.timeseries]]`` stanza. Raises on a
        malformed one; the caller decides whether that is fatal."""
        identity = entry.get("identity") or ()
        if isinstance(identity, str):
            identity = (identity,)
        rate = entry.get("rows_per_day")
        return cls(
            schema=str(entry["schema"]),
            table=str(entry["table"]),
            time_column=str(entry.get("time_column") or "ts"),
            identity=tuple(str(c) for c in identity),
            rows_per_day=int(rate) if rate is not None else None,
            chunk_interval=parse_duration(entry["chunk_interval"]) if entry.get("chunk_interval") else None,
            compress_after=parse_duration(entry.get("compress_after", DEFAULT_COMPRESS_AFTER)),
            retain=parse_duration(entry["retain"]) if entry.get("retain") else None,
            forward_guard=bool(entry.get("forward_guard", False)),
        )

    @property
    def qualified(self) -> str:
        return f"{self.schema}.{self.table}"

    @property
    def chunk(self) -> timedelta:
        return self.chunk_interval or derive_chunk_interval(self.rows_per_day)

    @property
    def segmentby(self) -> str:
        return ",".join(self.identity)

    @property
    def orderby(self) -> str:
        return f"{self.time_column} DESC"


@dataclass(frozen=True)
class ContractStatus:
    """The table as the catalogue describes it, in the contract's terms."""

    exists: bool
    is_hypertable: bool = False
    chunk_interval: timedelta | None = None
    compression_enabled: bool = False
    segmentby: str = ""
    orderby: str = ""
    compress_after: timedelta | None = None
    retain: timedelta | None = None
    retention_scheduled: bool | None = None
    retention_job_id: int | None = None
    estimated_rows: int = 0
    total_bytes: int = 0


def _norm(setting: str | None) -> str:
    return re.sub(r'[\s"]', "", setting or "")


def status(conn, contract: TimeseriesContract) -> ContractStatus:
    """Read the table's state back. Catalogue views only; never a count."""
    cur = conn.cursor()
    cur.execute("SELECT to_regclass(%s)", (contract.qualified,))
    if cur.fetchone()[0] is None:
        return ContractStatus(exists=False)
    cur.execute(
        "SELECT greatest(c.reltuples, coalesce(s.n_live_tup, 0))::bigint, "
        "       pg_total_relation_size(c.oid) "
        "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "LEFT JOIN pg_stat_user_tables s ON s.relid = c.oid "
        "WHERE n.nspname = %s AND c.relname = %s",
        (contract.schema, contract.table),
    )
    rows, total = cur.fetchone()
    cur.execute("SELECT 1 FROM pg_extension WHERE extname = 'timescaledb'")
    if cur.fetchone() is None:
        return ContractStatus(exists=True, estimated_rows=max(int(rows or 0), 0), total_bytes=int(total or 0))
    cur.execute(
        "SELECT time_interval FROM timescaledb_information.dimensions "
        "WHERE hypertable_schema = %s AND hypertable_name = %s AND column_name = %s",
        (contract.schema, contract.table, contract.time_column),
    )
    dim = cur.fetchone()
    if dim is None:
        return ContractStatus(exists=True, estimated_rows=max(int(rows or 0), 0), total_bytes=int(total or 0))
    cur.execute(
        "SELECT compression_enabled FROM timescaledb_information.hypertables "
        "WHERE hypertable_schema = %s AND hypertable_name = %s",
        (contract.schema, contract.table),
    )
    enabled = bool(cur.fetchone()[0])
    segmentby = orderby = ""
    if enabled:
        cur.execute(
            "SELECT segmentby, orderby FROM timescaledb_information.hypertable_compression_settings "
            "WHERE hypertable = %s::regclass",
            (contract.qualified,),
        )
        row = cur.fetchone()
        if row:
            segmentby, orderby = _norm(row[0]), (row[1] or "").strip()
    cur.execute(
        "SELECT proc_name, job_id, scheduled, (config->>'compress_after')::interval, "
        "       (config->>'drop_after')::interval "
        "FROM timescaledb_information.jobs "
        "WHERE hypertable_schema = %s AND hypertable_name = %s "
        "AND proc_name IN ('policy_compression', 'policy_retention')",
        (contract.schema, contract.table),
    )
    compress_after = retain = None
    scheduled = job_id = None
    for proc, jid, sched, after, drop in cur.fetchall():
        if proc == "policy_compression":
            compress_after = after
        else:
            retain, scheduled, job_id = drop, bool(sched), int(jid)
    cur.execute("SELECT hypertable_size(%s::regclass)", (contract.qualified,))
    total = cur.fetchone()[0] or total
    return ContractStatus(
        exists=True, is_hypertable=True, chunk_interval=dim[0],
        compression_enabled=enabled, segmentby=segmentby, orderby=orderby,
        compress_after=compress_after, retain=retain,
        retention_scheduled=scheduled, retention_job_id=job_id,
        estimated_rows=max(int(rows or 0), 0), total_bytes=int(total or 0),
    )


def unique_indexes_missing_time(cur, contract: TimeseriesContract) -> list[str]:
    """Unique indexes that omit the time column. TimescaleDB refuses each one,
    so the contract names them before trying."""
    cur.execute(
        "SELECT i.indexrelid::regclass::text FROM pg_index i "
        "JOIN pg_class c ON c.oid = i.indrelid "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = %s AND c.relname = %s AND i.indisunique "
        "AND NOT EXISTS (SELECT 1 FROM pg_attribute a WHERE a.attrelid = c.oid "
        "                AND a.attname = %s AND a.attnum = ANY (i.indkey)) "
        "ORDER BY 1",
        (contract.schema, contract.table, contract.time_column),
    )
    return [r[0] for r in cur.fetchall()]


def apply(conn, contract: TimeseriesContract, *, lock_timeout: str = DEFAULT_LOCK_TIMEOUT) -> list[str]:
    """Bring the table to the declaration. Idempotent; returns the steps taken.

    ``conn`` must be in autocommit mode: TimescaleDB policy calls cannot run
    inside a caller's transaction.
    """
    if not _LOCK_TIMEOUT.match(lock_timeout):
        raise ValueError(f"lock_timeout {lock_timeout!r} is not a Postgres duration")
    q = contract.qualified
    cur = conn.cursor()
    cur.execute(f"SET lock_timeout = '{lock_timeout}'")
    cur.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")
    st = status(conn, contract)
    if not st.exists:
        raise ContractRefused(f"{q} does not exist: the table's own DDL comes before its storage contract")
    steps: list[str] = []
    chunk = contract.chunk

    if not st.is_hypertable:
        missing = unique_indexes_missing_time(cur, contract)
        if missing:
            raise ContractRefused(
                f"{q}: unique index(es) {', '.join(missing)} omit the time column "
                f"{contract.time_column}, which TimescaleDB requires in every unique index. "
                f"Add {contract.time_column} to the key (it changes nothing about what is unique "
                "when the key already determines the time) and apply again."
            )
        if st.estimated_rows > IN_PLACE_ROW_LIMIT or st.total_bytes > IN_PLACE_BYTES_LIMIT:
            raise ContractRefused(
                f"{q} holds about {st.estimated_rows:,} rows ({st.total_bytes / 1024**3:.1f} GB): "
                "converting it in place would lock it for the length of a full copy. "
                f"Use `axi data adopt-timeseries {q}`, which copies by time window with "
                "the writers running and swaps names at the end."
            )
        cur.execute(
            "SELECT create_hypertable(%s, %s, chunk_time_interval => %s::interval, "
            "migrate_data => true)",
            (q, contract.time_column, _interval_sql(chunk)),
        )
        steps.append(f"{q} is a hypertable ({_human(chunk)} chunks)")
    elif st.chunk_interval != chunk:
        cur.execute("SELECT set_chunk_time_interval(%s::regclass, %s::interval)", (q, _interval_sql(chunk)))
        steps.append(f"{q}: new chunks are {_human(chunk)} wide (were {_human(st.chunk_interval)})")

    if not st.compression_enabled or st.segmentby != contract.segmentby or st.orderby != contract.orderby:
        segment = ", ".join(contract.identity)
        cur.execute(
            f"ALTER TABLE {q} SET (timescaledb.compress, "
            f"timescaledb.compress_segmentby = '{segment}', "
            f"timescaledb.compress_orderby = '{contract.orderby}')"
        )
        steps.append(
            f"{q}: compression segments by ({segment or 'nothing'}) and orders by {contract.orderby}"
            + ("" if not st.compression_enabled else f" (was ({st.segmentby}) / {st.orderby})")
        )

    if st.compress_after != contract.compress_after:
        if st.compress_after is not None:
            cur.execute("SELECT remove_compression_policy(%s::regclass, if_exists => true)", (q,))
        cur.execute(
            "SELECT add_compression_policy(%s::regclass, %s::interval, if_not_exists => true)",
            (q, _interval_sql(contract.compress_after)),
        )
        steps.append(
            f"{q}: compress chunks older than {_human(contract.compress_after)}"
            + (f" (was {_human(st.compress_after)})" if st.compress_after is not None else "")
        )

    if contract.retain is None:
        if st.retain is not None:
            cur.execute("SELECT remove_retention_policy(%s::regclass, if_exists => true)", (q,))
            steps.append(
                f"{q}: retention policy removed ({_human(st.retain)}): none is declared, "
                "and a policy nobody declared is one nobody checks"
            )
    elif st.retain != contract.retain:
        if st.retain is not None:
            cur.execute("SELECT remove_retention_policy(%s::regclass, if_exists => true)", (q,))
        cur.execute(
            "SELECT add_retention_policy(%s::regclass, %s::interval, if_not_exists => true)",
            (q, _interval_sql(contract.retain)),
        )
        job_id = cur.fetchone()[0]
        if contract.forward_guard:
            cur.execute("SELECT alter_job(%s, scheduled => false)", (job_id,))
            steps.append(
                f"{q}: drop rows older than {_human(contract.retain)}, installed paused: "
                "it runs once a pass confirms every batch is delivered upstream"
            )
        else:
            steps.append(f"{q}: drop rows older than {_human(contract.retain)}")
    return steps


def release_retention(conn, contract: TimeseriesContract, *, undelivered: int) -> str | None:
    """Pause or resume a guarded table's retention from the forwarder's state.

    ``undelivered`` is how many batches await delivery upstream (zero on a
    node that forwards nothing). Returns what changed, or ``None``.
    """
    if not contract.forward_guard or contract.retain is None:
        return None
    st = status(conn, contract)
    if st.retention_job_id is None:
        return None
    want = undelivered <= 0
    if st.retention_scheduled == want:
        return None
    conn.cursor().execute("SELECT alter_job(%s, scheduled => %s)", (st.retention_job_id, want))
    if want:
        return f"{contract.qualified}: retention resumed, every batch is delivered upstream"
    return (
        f"{contract.qualified}: retention paused, {undelivered} batch(es) await delivery "
        "upstream and nothing is dropped until they are"
    )


__all__ = [
    "CHUNK_LADDER",
    "DEFAULT_CHUNK_INTERVAL",
    "DEFAULT_COMPRESS_AFTER",
    "DEFAULT_LOCK_TIMEOUT",
    "IN_PLACE_BYTES_LIMIT",
    "IN_PLACE_ROW_LIMIT",
    "TARGET_ROWS_PER_CHUNK",
    "ContractRefused",
    "ContractStatus",
    "TimeseriesContract",
    "apply",
    "derive_chunk_interval",
    "parse_duration",
    "release_retention",
    "status",
    "unique_indexes_missing_time",
]
