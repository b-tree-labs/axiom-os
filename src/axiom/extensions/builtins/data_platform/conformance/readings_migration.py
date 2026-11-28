# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Convert an existing plain ``silver.signals`` into a compressed hypertable.

A fresh install gets the hypertable from the start
(:func:`.timeseries.ensure_readings_storage`). This is the path for an install
that already holds a large plain table (measured on one node: 201 GB, 290M
rows, ~115 GB of it indexes, no compression). Never run on startup: an
operator runs ``axi data readings-hypertable`` from a runbook, one phase at a
time, and every phase is idempotent and resumable.

**Why copy-beside, not ``create_hypertable(migrate_data => true)``.** The
in-place call keeps dependent views bound, but it holds an exclusive lock on
the table for the whole copy: on 290M rows that is hours with every reader
and writer blocked, and a failure part way rolls the whole copy back. The
platform's adoption (:mod:`..timeseries.adopt`, ADR-189) copies into a new
hypertable by time window, oldest first, one window per transaction, each
verified (row count AND an order-independent digest) before it is marked
done, compressing as it goes, with progress in a ledger, so a killed copy
resumes at its first window not done. Readers and the conform writer keep
running throughout. The swap is one short transaction: the tail written
since the last window is copied, the names swap, and every dependent view is
recreated in place (``CREATE OR REPLACE VIEW``) against the new table, so a
view built on a view keeps working. Writers wait only for that transaction
(seconds; the ledger records the measured hold). With 1.3 TB free, the space
cost is the compressed copy (~20-30 GB at the measured ratio) plus the old
table, which stays as ``silver.signals__old`` until ``drop_old``: it is the
rollback.

**The key.** The live key is ``(row_hash, channel)``; TimescaleDB requires the
time column in every unique index. ``row_hash`` hashes the whole bronze row,
``ts`` included, so ``(row_hash, channel, ts)`` is exactly as unique. It is
built ``CONCURRENTLY`` and swapped in with ``PRIMARY KEY USING INDEX``.

**The uncertainty foreign key** (terms table -> readings table ``(row_hash,
channel)``) is REPLACED, not extended: the terms table gains ``ts``
(backfilled in batches), its key becomes ``(row_hash, channel, ts, symbol)``,
and the foreign key is dropped. Why not a foreign key with ``ts``: a key
into the hypertable would follow the old table through the swap's rename
(it binds by identity), so it would have to be dropped and rebuilt anyway,
and TimescaleDB has checked one wrongly under a generic plan (2.17.2,
:mod:`.timeseries`). Integrity instead: the conform pass writes a reading's
terms in the same transaction as the reading, and :func:`orphan_terms` is
the checked join that ``finish`` and the runbook run (zero expected).

**No backup covers silver.signals** on the node this was written for (the
nightly dump excludes its data). So every phase that changes anything refuses
unless ``backup_file`` names a fresh custom-format dump of ``silver.signals``
(checked with ``pg_restore --list`` when available), or the operator accepts
re-deriving silver from bronze (``accept_rederive=True``). The refusal names
the dump command and estimates its size and duration from the table's heap.

Row counts are never one 290M-row ``count(*)``: the estimate reads the
table's own statistics (``reltuples``, ``n_live_tup``), and the copy counts
and digests each window as it goes.

Phases: ``estimate`` (default, writes nothing), ``prepare``, ``copy``,
``swap``, ``finish``. ``swap`` needs the conform writer paused
(``conform_paused``): the adoption's tail only covers times after its
boundary, and conform writes old timestamps, so rows written during the copy
into windows already copied are found by ``conformed_at`` (a BRIN built in
``prepare``) and copied before the names swap. Readers keep working
throughout; writers wait for that catch-up plus the swap transaction. Then the existing ``axi data adopt-timeseries
--table silver.signals --verify`` / ``--drop-old`` / ``--rollback``.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..silver_names import (
    SILVER_READING_TERMS,
    SILVER_READING_TERMS_NAME,
    SILVER_READINGS,
    SILVER_READINGS_NAME,
    SILVER_SCHEMA,
)
from .reading_identity import IDENTITY_INDEX
from .timeseries import TimeseriesPolicy, is_time_partitioned, readings_contract

_R, _T = SILVER_READINGS_NAME, SILVER_READING_TERMS_NAME
REDUNDANT_INDEX = f"{_R}_site_feed_channel_ts"
NEW_KEY_INDEX = f"{_R}_row_hash_channel_ts"
TIME_INDEX = f"{_R}_ts_brin"
#: conformed_at follows insertion order, so a BRIN finds rows written since a
#: moment without scanning the table: how the swap finds late writes.
CONFORMED_INDEX = f"{_R}_conformed_at_brin"
TERMS_KEY_INDEX = f"{_T}_row_hash_channel_ts_symbol"
TERMS_FK = f"{_T}_row_hash_channel_fkey"
TERMS_NN = f"{_T}_ts_nn"
#: A dump older than this is not "fresh".
BACKUP_MAX_AGE_S = 48 * 3600
#: Rows of the terms table given their ts per transaction.
BACKFILL_BATCH = 50_000
PHASES = ("estimate", "prepare", "copy", "swap", "finish")


class MigrationRefused(Exception):
    pass


@dataclass
class Facts:
    rows_estimate: int = 0
    live_rows: int = 0
    heap_bytes: int = 0
    total_bytes: int = 0
    hypertable: bool = False
    identity_valid: bool = False
    redundant_index: bool = False
    key_columns: list[str] = field(default_factory=list)
    terms_fk: bool = False
    terms_has_ts: bool = False
    dependent_views: int = 0
    rules: int = 0
    triggers: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


def _one(conn, sql: str, params: tuple = ()) -> Any:
    row = conn.execute(sql, params).fetchone()
    return None if row is None else row[0]


def facts(conn) -> Facts:
    """What the table is now, from the catalogue and its statistics only."""
    f = Facts()
    f.hypertable = is_time_partitioned(conn.cursor())
    row = conn.execute(
        "SELECT c.reltuples::bigint, coalesce(s.n_live_tup, 0), pg_relation_size(c.oid), "
        "pg_total_relation_size(c.oid) FROM pg_class c "
        "LEFT JOIN pg_stat_user_tables s ON s.relid = c.oid WHERE c.oid = %s::regclass",
        (SILVER_READINGS,),
    ).fetchone()
    f.rows_estimate, f.live_rows, f.heap_bytes, f.total_bytes = (max(int(x or 0), 0) for x in row)
    f.identity_valid = bool(_one(
        conn, "SELECT i.indisvalid FROM pg_index i WHERE i.indexrelid = to_regclass(%s)",
        (f"silver.{IDENTITY_INDEX}",)))
    f.redundant_index = _one(conn, "SELECT to_regclass(%s)", (f"silver.{REDUNDANT_INDEX}",)) is not None
    f.key_columns = [r[0] for r in conn.execute(
        "SELECT a.attname FROM pg_index i JOIN pg_attribute a ON a.attrelid = i.indrelid "
        "AND a.attnum = ANY(i.indkey) WHERE i.indrelid = %s::regclass AND i.indisprimary "
        "ORDER BY array_position(i.indkey::int2[], a.attnum)", (SILVER_READINGS,)).fetchall()]
    terms = _one(conn, "SELECT to_regclass(%s)", (SILVER_READING_TERMS,)) is not None
    if terms:
        f.terms_fk = _one(conn, "SELECT 1 FROM pg_constraint WHERE conname = %s AND conrelid = %s::regclass",
                          (TERMS_FK, SILVER_READING_TERMS)) is not None
        f.terms_has_ts = _one(conn, "SELECT 1 FROM information_schema.columns WHERE table_schema = 'silver' "
                              "AND table_name = %s AND column_name = 'ts'", (_T,)) is not None
    f.dependent_views = int(_one(
        conn, "SELECT count(DISTINCT r.ev_class) FROM pg_depend d JOIN pg_rewrite r ON r.oid = d.objid "
        "WHERE d.refobjid = %s::regclass AND r.ev_class <> %s::regclass", (SILVER_READINGS, SILVER_READINGS)) or 0)
    f.rules = int(_one(conn, "SELECT count(*) FROM pg_rules WHERE schemaname = %s AND tablename = %s", (SILVER_SCHEMA, _R))
                  or 0)
    f.triggers = [r[0] for r in conn.execute(
        "SELECT tgname FROM pg_trigger WHERE tgrelid = %s::regclass AND NOT tgisinternal",
        (SILVER_READINGS,)).fetchall()]
    return f


def dump_advice(f: Facts) -> str:
    """The dump command, and its size and time estimated from the heap."""
    gib = f.heap_bytes / 2**30
    return (
        f"pg_dump --format=custom --compress=6 --table={SILVER_READINGS} --table={SILVER_READING_TERMS} "
        f"--file=<data volume>/silver-{_R}-$(date +%F).dump <db>  "
        f"(heap {gib:.0f} GiB; expect a {gib * 0.15:.0f}-{gib * 0.3:.0f} GiB file and "
        f"~{max(gib / 60, 0.1):.1f}-{max(gib / 25, 0.2):.1f} h: one table dumps on one core; "
        "indexes are not dumped, only their definitions)"
    )


def backup_refusal(f: Facts, backup_file: str | None, accept_rederive: bool, *, now: float | None = None) -> str | None:
    """Why a changing phase may not run without a backup, or None."""
    if accept_rederive:
        return None
    if not backup_file:
        return ("no backup covers silver.signals (the nightly dump excludes its data). Pass backup_file=<a fresh "
                "custom-format dump>, or accept_rederive=true to accept rebuilding silver from bronze. "
                "To make the dump: " + dump_advice(f))
    p = Path(backup_file)
    if not p.is_file() or p.stat().st_size == 0:
        return f"backup_file {backup_file} does not exist or is empty. " + dump_advice(f)
    age = (time.time() if now is None else now) - p.stat().st_mtime
    if age > BACKUP_MAX_AGE_S:
        return f"backup_file {backup_file} is {age / 3600:.0f} h old; make a fresh one. " + dump_advice(f)
    if shutil.which("pg_restore"):
        out = subprocess.run(["pg_restore", "--list", str(p)], capture_output=True, text=True, timeout=300)
        if out.returncode != 0:
            return f"backup_file {backup_file} is not a readable custom-format dump: {out.stderr.strip()[:200]}"
        if not any(f"TABLE DATA {SILVER_SCHEMA} {_R}" in line for line in out.stdout.splitlines()):
            return f"backup_file {backup_file} has no TABLE DATA for silver.signals"
    return None


def orphan_terms(conn) -> int:
    """Uncertainty terms whose reading is gone: the check that replaces the
    foreign key. Zero is expected. Joins on the reading's key, so it is an
    index probe per term, not a scan of silver.signals."""
    if _one(conn, "SELECT to_regclass(%s)", (SILVER_READING_TERMS,)) is None:
        return 0
    # ADR-128 E5: one-time migration read of the silver tier (silver.signals and its terms).
    return int(_one(
        conn,
        f"SELECT count(*) FROM {SILVER_READING_TERMS} u WHERE NOT EXISTS (SELECT 1 FROM {SILVER_READINGS} s "
        "WHERE s.row_hash = u.row_hash AND s.channel = u.channel "
        "AND (u.ts IS NULL OR s.ts = u.ts))",
    ) or 0)


def _index_state(conn, name: str) -> bool | None:
    """True valid, False present but invalid (a failed CONCURRENTLY), None absent."""
    return _one(conn, "SELECT i.indisvalid FROM pg_index i WHERE i.indexrelid = to_regclass(%s)", (f"silver.{name}",))


def _build_concurrently(conn, name: str, ddl: str, say: Callable[[str], None]) -> str | None:
    state = _index_state(conn, name)
    if state is True:
        return None
    if state is False:
        conn.execute(f"DROP INDEX CONCURRENTLY IF EXISTS silver.{name}")
        say(f"  dropped invalid {name} left by an interrupted build")
    t = time.monotonic()
    say(f"  building {name} concurrently (reads and writes continue)")
    conn.execute(ddl)
    return f"built {name} in {time.monotonic() - t:.0f}s"


def _backfill_terms_ts(conn, say: Callable[[str], None]) -> int:
    """Give each term its reading's ts, in batches (one transaction each), so
    a long backfill neither holds locks nor pins the xmin horizon. Resumable:
    it only ever picks rows still NULL. A term with no reading stays NULL and
    is reported by :func:`orphan_terms`."""
    total = 0
    while True:
        # ADR-128 E5: one-time migration read of the silver tier while terms gain their reading's ts.
        n = conn.execute(
            f"UPDATE {SILVER_READING_TERMS} u SET ts = s.ts FROM {SILVER_READINGS} s "
            f"WHERE u.ctid = ANY(ARRAY(SELECT ctid FROM {SILVER_READING_TERMS} x WHERE x.ts IS NULL "
            "AND EXISTS (SELECT 1 FROM silver.signals y WHERE y.row_hash = x.row_hash AND y.channel = x.channel) "
            f"LIMIT {BACKFILL_BATCH})) AND s.row_hash = u.row_hash AND s.channel = u.channel"
        ).rowcount
        if not n:
            return total
        total += n
        say(f"  uncertainty terms given ts: {total:,}")


def prepare(conn, say: Callable[[str], None]) -> list[str]:
    """Everything the copy needs, on the live table, online. Idempotent."""
    steps: list[str] = []
    f = facts(conn)
    if not f.identity_valid:
        raise MigrationRefused(
            f"silver.{IDENTITY_INDEX} is missing or invalid; the conform writer needs it to stay on the "
            "identity key through the key change. Run the reading-identity migration first.")
    if f.redundant_index:
        conn.execute(f"DROP INDEX CONCURRENTLY IF EXISTS silver.{REDUNDANT_INDEX}")
        steps.append(f"dropped {REDUNDANT_INDEX}: a strict prefix of {IDENTITY_INDEX}")
    if f.key_columns != ["row_hash", "channel", "ts"]:
        done = _build_concurrently(conn, NEW_KEY_INDEX, f"CREATE UNIQUE INDEX CONCURRENTLY {NEW_KEY_INDEX} "
                                   f"ON {SILVER_READINGS} (row_hash, channel, ts)", say)
        if done:
            steps.append(done)
    if _one(conn, "SELECT to_regclass(%s)", (SILVER_READING_TERMS,)) is not None:
        conn.execute(f"ALTER TABLE {SILVER_READING_TERMS} ADD COLUMN IF NOT EXISTS ts timestamptz")
        n = _backfill_terms_ts(conn, say)
        if n:
            steps.append(f"gave {n:,} uncertainty term(s) their reading's ts")
        done = _build_concurrently(conn, TERMS_KEY_INDEX, f"CREATE UNIQUE INDEX CONCURRENTLY {TERMS_KEY_INDEX} "
                                   f"ON {SILVER_READING_TERMS} (row_hash, channel, ts, symbol)", say)
        if done:
            steps.append(done)
        if f.terms_fk:
            conn.execute(f"ALTER TABLE {SILVER_READING_TERMS} DROP CONSTRAINT IF EXISTS {TERMS_FK}")
            steps.append(f"replaced {TERMS_FK} with the ts-carrying key and the orphan_terms check")
    if f.key_columns != ["row_hash", "channel", "ts"]:
        # One short ACCESS EXCLUSIVE: a catalogue swap, the index already exists.
        conn.execute(f"ALTER TABLE {SILVER_READINGS} DROP CONSTRAINT {_R}_pkey, "
                     f"ADD CONSTRAINT {_R}_pkey PRIMARY KEY USING INDEX {NEW_KEY_INDEX}")
        steps.append("silver.signals key is now (row_hash, channel, ts)")
    if not conn.execute(
        "SELECT 1 FROM pg_index i JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = i.indkey[0] "
        "WHERE i.indrelid = %s::regclass AND a.attname = 'ts'", (SILVER_READINGS,)).fetchone():
        done = _build_concurrently(conn, TIME_INDEX, f"CREATE INDEX CONCURRENTLY {TIME_INDEX} "
                                   f"ON {SILVER_READINGS} USING brin (ts)", say)
        if done:
            steps.append(done + " (so each copy window ranges on time)")
    done = _build_concurrently(conn, CONFORMED_INDEX, f"CREATE INDEX CONCURRENTLY {CONFORMED_INDEX} "
                               f"ON {SILVER_READINGS} USING brin (conformed_at)", say)
    if done:
        steps.append(done + " (so the swap finds rows written into windows already copied)")
    return steps


def catch_late_writes(conn, say: Callable[[str], None]) -> int:
    """Copy rows written since the copy started into windows it had already
    done. The adoption's tail covers only times after its boundary; a conform
    pass writes old timestamps (a backlog, a late file), and those would be
    missing from the new table. Found by ``conformed_at`` (every write
    stamps it) through its BRIN, inserted with ON CONFLICT DO NOTHING. Run
    with the conform writer PAUSED, right before the swap, so nothing lands
    between this and the swap's lock."""
    started = _one(conn, "SELECT started_at FROM data_platform.timeseries_adoptions WHERE qualified = %s",
                   (SILVER_READINGS,))
    if started is None:
        raise MigrationRefused("no adoption copy is recorded; run phase copy first")
    # ADR-128 E5: one-time migration read of the silver tier (rows written during the copy).
    n = conn.execute(
        f"INSERT INTO {SILVER_READINGS}__adopt SELECT * FROM {SILVER_READINGS} "
        "WHERE conformed_at >= %s ON CONFLICT DO NOTHING", (started,)).rowcount
    say(f"  late writes copied: {n:,}")
    return int(n or 0)


def finish(conn, say: Callable[[str], None]) -> list[str]:
    """After the swap: the terms written during the copy get their ts, ts
    becomes NOT NULL (validated online), and the terms key gains ts."""
    steps: list[str] = []
    if _one(conn, "SELECT to_regclass(%s)", (SILVER_READING_TERMS,)) is None:
        return steps
    n = _backfill_terms_ts(conn, say)
    if n:
        steps.append(f"gave {n:,} uncertainty term(s) written during the copy their ts")
    orphans = orphan_terms(conn)
    if orphans:
        raise MigrationRefused(f"{orphans} uncertainty term(s) have no reading; resolve them before ts becomes "
                               "NOT NULL (they were orphaned before this migration: the foreign key cascaded)")
    if _one(conn, "SELECT is_nullable FROM information_schema.columns WHERE table_schema = 'silver' "
            "AND table_name = %s AND column_name = 'ts'", (_T,)) == "YES":
        conn.execute(f"ALTER TABLE {SILVER_READING_TERMS} ADD CONSTRAINT {TERMS_NN} "
                     "CHECK (ts IS NOT NULL) NOT VALID")
        conn.execute(f"ALTER TABLE {SILVER_READING_TERMS} VALIDATE CONSTRAINT {TERMS_NN}")
        conn.execute(f"ALTER TABLE {SILVER_READING_TERMS} ALTER COLUMN ts SET NOT NULL")
        conn.execute(f"ALTER TABLE {SILVER_READING_TERMS} DROP CONSTRAINT {TERMS_NN}")
        steps.append("silver.signal_uncertainty.ts is NOT NULL")
    if _index_state(conn, TERMS_KEY_INDEX) is True:
        conn.execute(f"ALTER TABLE {SILVER_READING_TERMS} DROP CONSTRAINT IF EXISTS {_T}_pkey, "
                     f"ADD CONSTRAINT {_T}_pkey PRIMARY KEY USING INDEX {TERMS_KEY_INDEX}")
        steps.append("silver.signal_uncertainty key is now (row_hash, channel, ts, symbol)")
    return steps


def migrate(
    conn,
    *,
    phase: str = "estimate",
    confirm: bool = False,
    backup_file: str | None = None,
    accept_rederive: bool = False,
    conform_paused: bool = False,
    policy: TimeseriesPolicy | None = None,
    max_windows: int | None = None,
    say: Callable[[str], None] = lambda _line: None,
    on_progress: Callable[[Any], None] | None = None,
) -> dict[str, Any]:
    """Run one phase. ``conn`` is a psycopg connection in autocommit mode."""
    from ..timeseries import adopt

    if phase not in PHASES:
        raise MigrationRefused(f"phase must be one of {', '.join(PHASES)}")
    if not conn.autocommit:
        raise MigrationRefused("the connection must be in autocommit mode")
    contract = readings_contract(policy or TimeseriesPolicy())
    f = facts(conn)
    out: dict[str, Any] = {"phase": phase, "facts": f.as_dict(), "steps": []}
    say(f"silver.signals: ~{f.rows_estimate:,} rows (reltuples), {f.live_rows:,} live (n_live_tup), "
        f"{f.total_bytes / 2**30:.1f} GiB with indexes, {f.dependent_views} dependent view(s), "
        f"{'hypertable' if f.hypertable else 'plain'}")
    if phase == "estimate":
        out["dump"] = dump_advice(f)
        if f.hypertable:
            out["steps"].append("already a hypertable: nothing to migrate (the contract is applied on deploy)")
            return out
        blockers = []
        if f.rules:
            blockers.append(f"{f.rules} rule(s) on silver.signals are not carried by the swap; drop and recreate")
        blockers += [f"trigger {t} is not carried by the swap" for t in f.triggers]
        out["blockers"] = blockers
        out["steps"].append(
            f"would: drop {REDUNDANT_INDEX} if present, key (row_hash, channel) -> (row_hash, channel, ts), "
            f"replace {TERMS_FK} with a ts-carrying key, copy by time window into a compressed hypertable, "
            "swap in one short transaction")
        return out
    if not confirm:
        raise MigrationRefused(f"phase {phase} changes silver.signals; pass confirm=true")
    refusal = backup_refusal(f, backup_file, accept_rederive)
    if refusal:
        raise MigrationRefused(refusal)
    if phase == "prepare":
        if f.rules or f.triggers:
            raise MigrationRefused("rules or triggers on silver.signals are not carried by the swap: "
                                   f"{f.rules} rule(s), triggers {f.triggers}")
        out["steps"] = prepare(conn, say)
    elif phase == "copy":
        if f.key_columns != ["row_hash", "channel", "ts"] or f.terms_fk:
            raise MigrationRefused("run phase prepare first: the key does not include ts yet")
        p = adopt.run(conn, contract, max_windows=max_windows, on_progress=on_progress)
        out["progress"] = p.as_dict()
        out["steps"].append(f"{p.windows_done}/{p.windows_total} windows copied and verified (count + digest "
                            f"per window), {p.rows_copied:,} rows")
    elif phase == "swap":
        if not conform_paused:
            raise MigrationRefused(
                "pause the conform writer first (its timer or watcher), then pass conform_paused=true: rows it "
                "writes into windows already copied are caught up before the names swap, and nothing may land "
                "between that and the swap. Readers keep working; resume conform when the swap reports done.")
        n = catch_late_writes(conn, say)
        out["steps"].append(f"copied {n:,} row(s) written into already-copied windows during the copy")
        out["steps"] += adopt.swap(conn, contract)
        out["steps"] += finish(conn, say)
    elif phase == "finish":
        out["steps"] = finish(conn, say)
    out["orphan_terms"] = orphan_terms(conn) if phase in ("swap", "finish") else None
    return out


__all__ = [
    "PHASES",
    "Facts",
    "MigrationRefused",
    "backup_refusal",
    "dump_advice",
    "facts",
    "finish",
    "migrate",
    "orphan_terms",
    "prepare",
]
