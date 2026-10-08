# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Keep watching a Postgres store after it is installed.

Every check here was written against a measurement taken on a live node on
2026-10-05, the first time the deployed data platform had been measured:

- ``axiom_db`` was 528 GB on a volume 91% full, growing 3.4 GB/day — about 90
  days of runway, and nothing reported it;
- its dominant object was 246 GB of table and 203 GB of index over 860,487,014
  rows, a plain unpartitioned table, while TimescaleDB 2.28.2 sat installed and
  idle. Measured compression on one real hour of that data: **113.9x**;
- ``shared_buffers`` was 128 MB on a host with 498 GB of RAM;
- ``n_dead_tup`` was 129,957,106 (13.1%) and the table had never been vacuumed,
  because the trigger was ``50 + 0.2 x 860,487,014`` = 172 M dead tuples.

Installation-time correctness is a different property from correctness three
months later, and only the second one matters to whoever has to answer a
question. The hygiene surface already reported disk *percentage*, which cannot
say when you run out; ``axiom.rag.storage_health`` already did this kind of work
but only ever for the retrieval store.

Checks are pure functions over fact dataclasses, so they are testable without a
database, and collecting the facts is somebody else's job. **Every check has a
negative control in the tests.** A check that cannot stay silent is ignored as
fast as one that cannot fire, and the difference is invisible in a green report.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from axiom.extensions.builtins.hygiene.node_health import Finding, Severity

CHECK_PREFIX = "store_health"

_GB = 1024**3
_MB = 1024**2

#: Below this share of host RAM, a buffer pool is too small to matter on a
#: server-class host. Postgres ships 128 MB regardless of the machine.
SHARED_BUFFERS_MIN_FRACTION = 0.10

#: Hosts smaller than this run a small buffer pool legitimately — a laptop or a
#: modest container should not be nagged about a server tuning.
SHARED_BUFFERS_SMALL_HOST_BYTES = 8 * _GB

#: Conventional target, and a cap. Past roughly this size the gains flatten and
#: checkpoint behaviour and double-buffering start to cost more than they return.
SHARED_BUFFERS_TARGET_FRACTION = 0.25
SHARED_BUFFERS_CAP_BYTES = 64 * _GB

#: Dead rows as a share of all rows. Ten percent is where a sequential scan is
#: reading one dead row in ten and the space is not being returned for reuse.
DEAD_TUPLE_WARN_FRACTION = 0.10

#: A table small enough that its bloat fraction is noise.
DEAD_TUPLE_MIN_ROWS = 1_000_000

#: An autovacuum trigger above this many dead tuples is not a trigger, it is a
#: deferred incident: by the time it fires the vacuum is large enough to compete
#: with live traffic for hours. Stock ``0.2`` is right for most tables and wrong
#: for a large one, which is why this is computed per relation rather than read
#: off the setting.
AUTOVACUUM_TRIGGER_CEILING = 20_000_000

#: What a suggested scale factor should aim to trigger at. Deriving the
#: suggestion from the ceiling instead produced 0.1889 for a table whose current
#: setting was 0.2 — arithmetically correct and useless, which reads as noise and
#: teaches an operator to skip the report.
AUTOVACUUM_TARGET_DEAD_ROWS = 5_000_000

#: Below this, how a timeseries table is physically organised does not matter
#: enough to raise with an operator.
TIMESERIES_MIN_BYTES = 10 * _GB

#: Runway thresholds in days. Six months is enough notice to plan a migration;
#: a month is not enough to do anything but react.
DAYS_TO_FULL_WARN = 180
DAYS_TO_FULL_CRITICAL = 30


@dataclass(frozen=True)
class RelationFacts:
    """One table as the catalog describes it."""

    schema: str
    name: str
    rows: int
    table_bytes: int
    index_bytes: int
    dead_tuples: int
    ever_vacuumed: bool
    is_hypertable: bool
    compression_enabled: bool
    #: Whether a timestamp column on this table participates in any index.
    #:
    #: Two weaker signals were tried against a live store first. Size alone
    #: recommended hypertable conversion for a RAG chunk table and a file
    #: manifest. *Having* a timestamp column was no better — almost every table
    #: carries a ``created_at``, and all ten examined passed it.
    #:
    #: Somebody indexes time because they query time ranges, which is what makes
    #: a table a timeseries. On the live store this separated the two real ones
    #: (both with a column named ``ts``, one of them in the primary key) from
    #: ``fetched_at``, ``indexed_at`` and ``updated_at`` audit columns. It is a
    #: heuristic, not a declaration: a consumer that knows which of its tables
    #: are timeseries should say so rather than leave this to be inferred.
    time_column_indexed: bool = False

    @property
    def qualified(self) -> str:
        return f"{self.schema}.{self.name}"

    @property
    def total_bytes(self) -> int:
        return self.table_bytes + self.index_bytes


@dataclass(frozen=True)
class ServerFacts:
    """Server-wide settings, read from ``pg_settings`` and the host.

    ``effective_memory_bytes`` is the memory the database may actually use —
    the container or cgroup limit where one exists, and host RAM only when it
    does not. The distinction is not pedantic: on the node this was written
    against, the host had 498 GB of RAM and the Postgres pod had a 16 GiB limit,
    so sizing a buffer pool against host RAM would have recommended a value that
    OOM-kills the database. A check that gives dangerous advice is worse than no
    check.
    """

    shared_buffers_bytes: int
    effective_memory_bytes: int | None
    work_mem_bytes: int
    maintenance_work_mem_bytes: int
    autovacuum_enabled: bool
    autovacuum_scale_factor: float
    autovacuum_threshold: int
    timescale_installed: bool


@dataclass(frozen=True)
class VolumeFacts:
    """A filesystem holding data, and how fast it is filling.

    ``daily_growth_bytes`` is None when growth has not been measured. It stays
    None rather than defaulting to zero, because a forecast built on an assumed
    rate is worse than no forecast.
    """

    mount: str
    total_bytes: int
    free_bytes: int
    daily_growth_bytes: float | None = None
    #: What the deployment *claims* this volume is, when anything claims it —
    #: a PersistentVolume's ``capacity.storage``, for instance. None when no
    #: declaration exists to compare against.
    declared_capacity_bytes: int | None = None


@dataclass
class StoreFacts:
    server: ServerFacts
    relations: list[RelationFacts] = field(default_factory=list)
    volumes: list[VolumeFacts] = field(default_factory=list)


def _human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} TB"


def _f(check: str, severity: Severity, message: str, current: str = "",
       expected: str = "", auto_fixable: bool = False) -> Finding:
    return Finding(
        check=f"{CHECK_PREFIX}_{check}",
        severity=severity,
        message=message,
        current_value=current,
        expected_value=expected,
        auto_fixable=auto_fixable,
    )


#: Postgres reads the host, not its own container, so every memory setting it
#: ships is wrong inside one. Sizing is derived here from a single number — the
#: memory the database may actually use.
#: A planner hint, not an allocation: how much of the data the OS is likely to be
#: caching. Three quarters of the limit matched what an operator had already set
#: on the live node (12GB of 16Gi), and the first version of this function, at
#: one half, would have lowered it.
EFFECTIVE_CACHE_FRACTION = 0.75
#: Used only by VACUUM, index builds and similar, a few at a time. The first
#: version gave 819MB for a 16Gi limit where the node already ran 1GB — a
#: recommendation that would have been a downgrade.
MAINTENANCE_WORK_MEM_FRACTION = 0.125
MAINTENANCE_WORK_MEM_CAP_BYTES = 2 * _GB
#: work_mem is per sort, per connection, so the budget that matters is all of
#: them sorting at once. The first version sized it from the limit alone and
#: produced 64MB for a 16Gi pod: 6.2 GB across 100 connections, on top of a 4GB
#: buffer pool. This bounds every connection's sort together to a fifth of the
#: limit, in 8MB steps.
WORK_MEM_TOTAL_FRACTION = 0.20
WORK_MEM_STEP_BYTES = 8 * _MB
WORK_MEM_FLOOR_BYTES = 4 * _MB
WORK_MEM_CAP_BYTES = 64 * _MB
DEFAULT_MAX_CONNECTIONS = 100
#: Low enough that a large table is actually reached, while the absolute
#: threshold still stops small tables vacuuming constantly.
RECOMMENDED_AUTOVACUUM_SCALE_FACTOR = 0.02


def _pg_size(n: int) -> str:
    """Bytes as Postgres writes them in a config value."""
    if n >= _GB and n % _GB == 0:
        return f"{n // _GB}GB"
    return f"{max(round(n / _MB), 1)}MB"


def recommended_postgres_settings(
    memory_limit_bytes: int, max_connections: int = DEFAULT_MAX_CONNECTIONS
) -> dict[str, str]:
    """Settings for a Postgres that may use ``memory_limit_bytes``.

    The argument is the container or cgroup limit, never host RAM. On the node
    this was written against, the host had 498 GB and the pod was capped at
    16 GiB; sizing against the host would have produced a buffer pool that
    OOM-kills the database on first use.

    For a 16Gi limit and 100 connections this returns exactly what was applied
    and verified on that node: shared_buffers 4GB, effective_cache_size 12GB,
    maintenance_work_mem 2GB, work_mem 32MB.

    One function, three callers: the health check quotes it as the value it
    expected, and both generators that create a Postgres — the data-platform
    chart and the signals bootstrap — are tested against it. Settings derived
    separately from the limit they run under are how a database gets
    OOM-killed at start.
    """
    limit = max(int(memory_limit_bytes), 0)
    connections = max(int(max_connections), 1)
    shared = min(int(limit * SHARED_BUFFERS_TARGET_FRACTION), SHARED_BUFFERS_CAP_BYTES)
    maintenance = min(int(limit * MAINTENANCE_WORK_MEM_FRACTION), MAINTENANCE_WORK_MEM_CAP_BYTES)
    per_connection = int(limit * WORK_MEM_TOTAL_FRACTION) // connections
    work = (per_connection // WORK_MEM_STEP_BYTES) * WORK_MEM_STEP_BYTES
    work = max(min(work, WORK_MEM_CAP_BYTES), WORK_MEM_FLOOR_BYTES)
    return {
        "shared_buffers": _pg_size(shared),
        "effective_cache_size": _pg_size(int(limit * EFFECTIVE_CACHE_FRACTION)),
        "maintenance_work_mem": _pg_size(maintenance),
        "work_mem": _pg_size(work),
        "autovacuum_vacuum_scale_factor": str(RECOMMENDED_AUTOVACUUM_SCALE_FACTOR),
    }


def check_shared_buffers(server: ServerFacts) -> Finding | None:
    """Postgres ships 128 MB whatever the machine, and nothing re-reads it."""
    if server.effective_memory_bytes is None:
        # Saying nothing here would be the worst outcome: the check appears to
        # pass, and a buffer pool sized for a different machine stays wrong
        # forever. An unanswerable question is reported as unanswered.
        return _f(
            "shared_buffers_unknown_limit",
            Severity.INFO,
            "Buffer-pool sizing was not checked: the memory limit the database "
            "actually runs under could not be established from here. This is "
            "normal when the audit runs outside the database's container — host "
            "RAM is not the limit that OOM-kills it. Set "
            "AXIOM_DB_MEMORY_LIMIT_BYTES to the container or cgroup limit.",
            current="limit unknown",
            expected="AXIOM_DB_MEMORY_LIMIT_BYTES set, or the audit run alongside the database",
        )
    if server.effective_memory_bytes < SHARED_BUFFERS_SMALL_HOST_BYTES:
        return None
    share = server.shared_buffers_bytes / max(server.effective_memory_bytes, 1)
    if share >= SHARED_BUFFERS_MIN_FRACTION:
        return None
    target = recommended_postgres_settings(server.effective_memory_bytes)["shared_buffers"]
    return _f(
        "shared_buffers",
        Severity.WARNING,
        f"shared_buffers is {_human(server.shared_buffers_bytes)} against "
        f"{_human(server.effective_memory_bytes)} of usable memory — "
        f"{share * 100:.2f}%. "
        "That is the stock default, which is chosen without reference to the "
        "machine. Queries fall through to the OS page cache for everything.",
        current=f"{_human(server.shared_buffers_bytes)} ({share * 100:.2f}% of "
                f"{_human(server.effective_memory_bytes)})",
        expected=f"around {target}",
    )


def check_dead_tuples(rel: RelationFacts) -> Finding | None:
    """Bloat, and whether anything has ever reclaimed it."""
    if rel.rows < DEAD_TUPLE_MIN_ROWS:
        return None
    live_plus_dead = rel.rows + rel.dead_tuples
    if live_plus_dead <= 0:
        return None
    share = rel.dead_tuples / live_plus_dead
    if share < DEAD_TUPLE_WARN_FRACTION:
        return None
    wasted = int(rel.table_bytes * share)
    if not rel.ever_vacuumed:
        return _f(
            "dead_tuples_vacuum",
            Severity.CRITICAL,
            f"{rel.qualified} is {share * 100:.1f}% dead tuples "
            f"({rel.dead_tuples:,} rows, about {_human(wasted)}) and has never "
            "been vacuumed — neither last_vacuum nor last_autovacuum is set. The "
            "space is not available for reuse, and the first vacuum will be "
            "large enough to compete with live traffic.",
            current=f"{share * 100:.1f}% dead, never vacuumed",
            expected=f"under {DEAD_TUPLE_WARN_FRACTION * 100:.0f}%, vacuumed routinely",
        )
    return _f(
        "dead_tuples_vacuum",
        Severity.WARNING,
        f"{rel.qualified} is {share * 100:.1f}% dead tuples "
        f"({rel.dead_tuples:,} rows, about {_human(wasted)}). Vacuum is not "
        "keeping up with the write rate.",
        current=f"{share * 100:.1f}% dead",
        expected=f"under {DEAD_TUPLE_WARN_FRACTION * 100:.0f}%",
    )


def check_autovacuum_reachable(rel: RelationFacts, server: ServerFacts) -> Finding | None:
    """Whether this table's autovacuum trigger is one it will reach usefully.

    The subtle failure: autovacuum is on, correctly configured by its own
    defaults, and will still never fire on a large table before the resulting
    vacuum is itself an incident. Nothing reports a threshold that is merely
    too far away.
    """
    if not server.autovacuum_enabled:
        return _f(
            "autovacuum_disabled",
            Severity.CRITICAL,
            "autovacuum is switched off. Dead tuples will accumulate until a "
            "manual vacuum runs, and transaction-id wraparound becomes a "
            "question of when.",
            current="off",
            expected="on",
        )
    if rel.rows < DEAD_TUPLE_MIN_ROWS:
        return None
    trigger = int(server.autovacuum_threshold + server.autovacuum_scale_factor * rel.rows)
    if trigger <= AUTOVACUUM_TRIGGER_CEILING:
        return None
    suggested = max(round(AUTOVACUUM_TARGET_DEAD_ROWS / max(rel.rows, 1), 4), 0.0001)
    return _f(
        "autovacuum_trigger_unreachable",
        Severity.WARNING,
        f"{rel.qualified} will not autovacuum until {trigger:,} rows are dead "
        f"(threshold {server.autovacuum_threshold} + scale_factor "
        f"{server.autovacuum_scale_factor} x {rel.rows:,} rows). Stock settings "
        "are right for most tables and wrong for one this size: by the time the "
        "trigger is met the vacuum is large enough to disrupt serving. Set a "
        "per-table override.",
        current=f"{trigger:,} dead rows to trigger",
        expected=f"autovacuum_vacuum_scale_factor = {suggested} on this table",
    )


def check_timeseries_storage(rel: RelationFacts, server: ServerFacts) -> Finding | None:
    """A large append-only table stored as a plain heap, where the extension
    that would compress it is already installed.

    Measured on this shape: 113.9x, 134.3 bytes/row down to 1.18.
    """
    if not server.timescale_installed:
        return None
    if rel.total_bytes < TIMESERIES_MIN_BYTES:
        return None
    if not rel.time_column_indexed and not rel.is_hypertable:
        # Large, but nothing suggests a time dimension worth partitioning on.
        return None
    if not rel.is_hypertable:
        return _f(
            "timeseries_not_a_hypertable",
            Severity.WARNING,
            f"{rel.qualified} holds {_human(rel.total_bytes)} over {rel.rows:,} "
            "rows as a plain table, and TimescaleDB is installed. Converting it "
            "to a hypertable with compression on chunks older than the "
            "deduplication window is the single largest reclaim available. "
            "Compression measured on data of this shape: 113.9x.",
            current=f"plain table, {_human(rel.total_bytes)}",
            expected="hypertable with a compression policy",
        )
    if not rel.compression_enabled:
        return _f(
            "timeseries_not_compressed",
            Severity.WARNING,
            f"{rel.qualified} is a hypertable of {_human(rel.total_bytes)} with "
            "compression disabled. Chunks past the deduplication window can be "
            "compressed without affecting ingest.",
            current=f"compression off, {_human(rel.total_bytes)}",
            expected="timescaledb.compress enabled with a compress_after policy",
        )
    return None


def check_days_to_full(volume: VolumeFacts) -> Finding | None:
    """A percentage cannot tell an operator when they run out of room."""
    rate = volume.daily_growth_bytes
    if not rate or rate <= 0:
        return None
    days = volume.free_bytes / rate
    if days >= DAYS_TO_FULL_WARN:
        return None
    severity = Severity.CRITICAL if days < DAYS_TO_FULL_CRITICAL else Severity.WARNING
    used_pct = 100.0 * (1 - volume.free_bytes / max(volume.total_bytes, 1))
    return _f(
        "days_to_full",
        severity,
        f"{volume.mount} has {_human(volume.free_bytes)} free and is growing "
        f"{_human(rate)}/day — about {days:.0f} days of runway. It is "
        f"{used_pct:.0f}% used, which on its own says nothing about when it ends.",
        current=f"{days:.0f} days at {_human(rate)}/day",
        expected=f"more than {DAYS_TO_FULL_WARN} days",
    )


#: A volume may exceed its declared capacity by this factor before it is worth
#: saying so. Some slack is normal; an order of magnitude is a declaration that
#: has stopped describing anything.
DECLARED_CAPACITY_TOLERANCE = 1.5


def check_declared_capacity(volume: VolumeFacts, used_bytes: int) -> Finding | None:
    """A declared capacity that nothing enforces and nothing compares.

    This is the check that would have caught the whole situation earliest. The
    PersistentVolume behind a 528 GB database declared **10Gi**, and because the
    ``local-path`` provisioner creates a host directory and ignores the requested
    size, nothing stopped it and nothing reported it. ``kubectl get pvc`` showed
    ``10Gi  Bound`` — healthy, by every surface anyone was looking at, while the
    volume had grown fifty times past its own declaration.

    An unenforced declaration is not a limit. It is a comment.
    """
    declared = volume.declared_capacity_bytes
    if not declared or declared <= 0 or used_bytes <= 0:
        return None
    if used_bytes <= declared * DECLARED_CAPACITY_TOLERANCE:
        return None
    return _f(
        "declared_capacity_exceeded",
        Severity.CRITICAL,
        f"{volume.mount} holds {_human(used_bytes)} against a declared capacity "
        f"of {_human(declared)} — {used_bytes / declared:.0f}x over. Whatever "
        "provisioned this volume does not enforce the size it was asked for, so "
        "the declaration describes nothing and no surface reports the gap. Either "
        "declare the real size or use a storage class that enforces one.",
        current=f"{_human(used_bytes)} used, {_human(declared)} declared",
        expected="a declared capacity that matches reality, or one that is enforced",
    )


def run_all(facts: StoreFacts) -> list[Finding]:
    """Every check, in severity-independent order. Never raises."""
    out: list[Finding] = []
    buffers = check_shared_buffers(facts.server)
    if buffers:
        out.append(buffers)
    for rel in facts.relations:
        for finding in (
            check_dead_tuples(rel),
            check_autovacuum_reachable(rel, facts.server),
            check_timeseries_storage(rel, facts.server),
        ):
            if finding:
                out.append(finding)
    used = sum(rel.total_bytes for rel in facts.relations)
    for volume in facts.volumes:
        for finding in (
            check_days_to_full(volume),
            check_declared_capacity(volume, used),
        ):
            if finding:
                out.append(finding)
    return out


__all__ = [
    "CHECK_PREFIX",
    "RelationFacts",
    "ServerFacts",
    "StoreFacts",
    "VolumeFacts",
    "check_autovacuum_reachable",
    "check_days_to_full",
    "check_declared_capacity",
    "check_dead_tuples",
    "check_shared_buffers",
    "check_timeseries_storage",
    "recommended_postgres_settings",
    "run_all",
]
