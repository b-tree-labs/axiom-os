# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A populated plain table is adopted into the time-series contract in place.

Lock-light and resumable: the rows are copied by time window into a new
hypertable with the writer running, every window is verified (row count and
an order-independent hash) before it is marked done, the names swap in one
short transaction, and the old table stays until a final verification and an
explicit drop. A deliberately corrupted window is caught, both during the copy
and before the drop.

Against a real TimescaleDB (a ``tsdb-*`` container; never the developer's
database). The synthetic table is ``AXIOM_TSDB_ADOPT_ROWS`` rows (default two
million, sized for CI); ``AXIOM_TSDB_SCALE_ROWS`` opts in to the tens-of-
millions proof, which prints what it measured.
"""

from __future__ import annotations

import os
import threading
import time
from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from axiom.extensions.builtins.data_platform.timeseries import adopt
from axiom.extensions.builtins.data_platform.timeseries.contract import (
    TimeseriesContract,
    status,
)

from .conftest import SCHEMA

ROWS = int(os.environ.get("AXIOM_TSDB_ADOPT_ROWS") or 2_000_000)
CHANNELS = ("c1", "c2", "c3")
WINDOW = timedelta(hours=12)
D = timedelta(days=1)


def _contract(**over) -> TimeseriesContract:
    base = dict(schema=SCHEMA, table="readings", identity=("site", "feed", "channel"),
                chunk_interval=WINDOW)
    base.update(over)
    return TimeseriesContract(**base)


def _create_plain(db, rows: int = ROWS, *, time_index: bool = True, table: str = "readings"):
    """A plain table shaped like the one that motivated this: identity text
    columns, a timestamp, a double, a unique key that includes the time, and
    the rows ending a couple of hours ago so the live window is empty."""
    db.execute(
        f"CREATE TABLE {SCHEMA}.{table} (site text NOT NULL, feed text NOT NULL, "
        f"channel text NOT NULL, ts timestamptz NOT NULL, value double precision, "
        f"UNIQUE (site, feed, channel, ts))"
    )
    if time_index:
        db.execute(f"CREATE INDEX {table}_ts ON {SCHEMA}.{table} (ts)")
    per_channel = rows // len(CHANNELS)
    end = datetime.now(UTC) - timedelta(hours=2)
    db.execute(
        f"INSERT INTO {SCHEMA}.{table} SELECT 'a', 'f', ch, %s - make_interval(secs => i), "
        f"20.0 + (i %% 50) * 0.01 FROM generate_series(0, %s) i, unnest(%s::text[]) ch",
        (end, per_channel - 1, list(CHANNELS)),
    )
    db.execute(f"ANALYZE {SCHEMA}.{table}")
    return per_channel * len(CHANNELS)


def _count(db, table="readings") -> int:
    return db.execute(f"SELECT count(*) FROM {SCHEMA}.{table}").fetchone()[0]


def _ledger(db):
    db.execute("DROP TABLE IF EXISTS data_platform.timeseries_adoption_windows")
    db.execute("DROP TABLE IF EXISTS data_platform.timeseries_adoptions")


@pytest.fixture
def plain(db):
    _ledger(db)
    n = _create_plain(db)
    db.execute(f"CREATE VIEW {SCHEMA}.latest AS SELECT channel, max(ts) AS ts FROM {SCHEMA}.readings GROUP BY channel")
    db.execute("DROP ROLE IF EXISTS ts_reader")
    db.execute("CREATE ROLE ts_reader")
    db.execute(f"GRANT SELECT ON {SCHEMA}.readings TO ts_reader")
    return n


# -- estimate -----------------------------------------------------------------


def test_the_estimate_says_rows_bytes_windows_ratio_and_eta_and_writes_nothing(db, plain):
    est = adopt.estimate(db, _contract(), sample_rows=50_000)
    assert est.feasible, est.blockers
    assert est.rows == pytest.approx(plain, rel=0.05), "a planner estimate, never a count"
    assert est.bytes > 0
    assert est.windows >= 10 and est.window == WINDOW
    assert est.sample_rows >= 40_000 and est.ratio > 3, (est.sample_rows, est.ratio)
    assert est.expected_bytes < est.bytes / 3
    assert est.eta_seconds > 0 and est.rows_per_second > 0
    assert est.dependent_views == [f"{SCHEMA}.latest"]
    assert not status(db, _contract()).is_hypertable
    assert db.execute(f"SELECT to_regclass('{SCHEMA}.readings__adopt')").fetchone()[0] is None
    assert db.execute(f"SELECT to_regclass('{SCHEMA}.readings__sample')").fetchone()[0] is None


def test_the_estimate_names_what_blocks_the_adoption(db):
    _ledger(db)
    _create_plain(db, 10_000, time_index=False)
    db.execute(f"CREATE TABLE {SCHEMA}.notes (ts timestamptz, site text, feed text, channel text, "
               f"FOREIGN KEY (site, feed, channel, ts) REFERENCES {SCHEMA}.readings (site, feed, channel, ts))")
    db.execute(f"CREATE FUNCTION {SCHEMA}.noop() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RETURN NEW; END $$")
    db.execute(f"CREATE TRIGGER t BEFORE INSERT ON {SCHEMA}.readings FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.noop()")
    est = adopt.estimate(db, _contract())
    assert not est.feasible
    text = " ".join(est.blockers)
    assert "index" in text and "brin" in text.lower(), "the lock-light index is the remedy named"
    assert "notes" in text, "a foreign key would follow the old table through the rename"
    assert "trigger" in text


def test_an_already_adopted_table_is_not_estimated_twice(db):
    _ledger(db)
    _create_plain(db, 1_000)
    from axiom.extensions.builtins.data_platform.timeseries.contract import apply

    apply(db, _contract())
    est = adopt.estimate(db, _contract())
    assert not est.feasible and "already" in " ".join(est.blockers)


# -- the copy -----------------------------------------------------------------


def test_the_copy_verifies_each_window_and_resumes_where_it_stopped(db, plain):
    first = adopt.run(db, _contract(), max_windows=3)
    assert first.phase == "copying" and first.windows_done == 3 and first.windows_total >= 10
    assert first.rows_copied > 0 and first.rows_per_second > 0 and first.eta_seconds > 0
    copied_before = _count(db, "readings__adopt")
    # "Interrupted": a fresh call with no memory of the first one.
    second = adopt.run(db, _contract())
    assert second.windows_done == second.windows_total
    assert second.failed_window is None
    # Everything before the boundary; the live window is the swap's tail.
    before_boundary = db.execute(
        f"SELECT count(*) FROM {SCHEMA}.readings WHERE ts < %s", (second.boundary,)).fetchone()[0]
    assert plain - before_boundary < plain * 0.2, "the tail is the live window, not the table"
    assert _count(db, "readings__adopt") == before_boundary
    assert _count(db, "readings__adopt") > copied_before
    ledger = db.execute(
        "SELECT count(*), sum(rows) FROM data_platform.timeseries_adoption_windows WHERE qualified = %s",
        (f"{SCHEMA}.readings",),
    ).fetchone()
    assert ledger == (second.windows_total, before_boundary)
    # The verified windows were compressed behind the copy.
    compressed = db.execute(
        "SELECT count(*) FROM timescaledb_information.chunks WHERE hypertable_schema = %s "
        "AND hypertable_name = 'readings__adopt' AND is_compressed", (SCHEMA,)).fetchone()[0]
    assert compressed >= second.windows_total - 1
    assert adopt.run(db, _contract()).windows_done == second.windows_total, "a third run has nothing to do"


def test_a_window_corrupted_during_the_copy_is_caught_and_not_marked_done(db, plain):
    """The verification must be able to fail. A copy whose check cannot fail
    proves nothing, so a row is deliberately changed between the copy and
    the check of the second window."""
    struck = {}

    def tamper(conn, contract, lo, hi, n):
        if n == 1:
            struck["window"] = (lo, hi)
            # Bounded by the window, so chunk exclusion keeps the UPDATE off
            # the previous window's chunk, which is already compressed. With
            # only `ts = (subquery)` the planner cannot exclude it and
            # TimescaleDB decompresses it to look: over the 100,000-tuple DML
            # limit when that chunk holds a full window (CI, 2026-10-15).
            conn.execute(
                f"UPDATE {SCHEMA}.readings__adopt SET value = value + 1000 "
                f"WHERE ts >= %s AND ts < %s "
                f"AND ts = (SELECT min(ts) FROM {SCHEMA}.readings__adopt WHERE ts >= %s AND ts < %s)",
                (lo, hi, lo, hi),
            )

    p = adopt.run(db, _contract(), max_windows=5, _after_copy=tamper)
    assert p.failed_window == struck["window"]
    assert p.windows_done == 1, "the first window is done; the corrupted one is not"
    assert "hash" in (p.failure or "") or "count" in (p.failure or "")
    lo, hi = struck["window"]
    assert db.execute(
        f"SELECT count(*) FROM {SCHEMA}.readings__adopt WHERE ts >= %s AND ts < %s", (lo, hi)
    ).fetchone()[0] == 0, "the failed window's transaction rolled back"
    assert db.execute(
        "SELECT count(*) FROM data_platform.timeseries_adoption_windows WHERE qualified = %s AND window_start = %s",
        (f"{SCHEMA}.readings", lo),
    ).fetchone()[0] == 0
    # Without the corruption the same window copies and verifies.
    again = adopt.run(db, _contract())
    assert again.failed_window is None and again.windows_done == again.windows_total
    assert _count(db, "readings__adopt") == db.execute(
        f"SELECT count(*) FROM {SCHEMA}.readings WHERE ts < %s", (again.boundary,)).fetchone()[0]


# -- the swap -----------------------------------------------------------------


class _Writer(threading.Thread):
    """A live writer through the table's NAME, as the real one writes."""

    def __init__(self, dsn: str) -> None:
        super().__init__(daemon=True)
        self.dsn, self.rows, self.errors, self._halt = dsn, 0, [], threading.Event()

    def run(self) -> None:
        with psycopg.connect(self.dsn, autocommit=True) as c:
            while not self._halt.is_set():
                try:
                    c.execute(
                        f"INSERT INTO {SCHEMA}.readings VALUES ('a', 'f', 'live', now(), 1.0) "
                        "ON CONFLICT DO NOTHING"
                    )
                    self.rows += 1
                except Exception as exc:  # noqa: BLE001 - the test reads them
                    self.errors.append(repr(exc))
                time.sleep(0.005)

    def stop(self) -> None:
        self._halt.set()
        self.join(timeout=10)


def test_the_swap_is_one_short_transaction_and_the_writer_never_stops(timescale_dsn, db, plain):
    writer = _Writer(timescale_dsn)
    writer.start()
    time.sleep(0.2)
    adopt.run(db, _contract())
    steps = adopt.swap(db, _contract())
    time.sleep(0.3)
    writer.stop()
    assert not writer.errors, writer.errors[:3]
    assert writer.rows > 20
    # The name now is the hypertable, with everything the old table had and
    # everything written since.
    assert status(db, _contract()).is_hypertable
    assert _count(db) == plain + writer.rows
    assert _count(db, "readings__old") >= plain
    assert db.execute(f"SELECT to_regclass('{SCHEMA}.readings__adopt')").fetchone()[0] is None
    # Dependent views follow the name, not the old table's identity.
    assert "readings__old" not in db.execute(f"SELECT pg_get_viewdef('{SCHEMA}.latest'::regclass)").fetchone()[0]
    assert db.execute(f"SELECT count(*) FROM {SCHEMA}.latest").fetchone()[0] == len(CHANNELS) + 1
    # Grants carried across.
    assert db.execute(
        "SELECT has_table_privilege('ts_reader', %s, 'SELECT')", (f"{SCHEMA}.readings",)).fetchone()[0]
    assert any("swapped" in s for s in steps), steps
    assert adopt.progress(db, _contract()).phase == "swapped"


def test_swap_refuses_while_windows_remain(db, plain):
    adopt.run(db, _contract(), max_windows=2)
    with pytest.raises(adopt.AdoptionRefused):
        adopt.swap(db, _contract())


# -- verification, the drop, and rollback -------------------------------------


def test_the_old_table_is_dropped_only_after_a_final_verification(db, plain):
    adopt.run(db, _contract())
    adopt.swap(db, _contract())
    with pytest.raises(adopt.AdoptionRefused):
        adopt.drop_old(db, _contract())
    v = adopt.verify(db, _contract())
    assert v.ok and v.windows_checked >= 10 and v.missing_rows == 0
    steps = adopt.drop_old(db, _contract())
    assert db.execute(f"SELECT to_regclass('{SCHEMA}.readings__old')").fetchone()[0] is None
    assert any("dropped" in s for s in steps)
    assert adopt.progress(db, _contract()).phase == "dropped"


def test_a_row_lost_from_the_copy_is_found_before_the_drop_and_repaired(db, plain):
    adopt.run(db, _contract())
    adopt.swap(db, _contract())
    # Silent damage to the new table after the copy: one row changed, one gone.
    # Both sit in compressed chunks, so the damage itself has to lift the
    # per-transaction decompression cap, as any out-of-band write would.
    db.execute("SET timescaledb.max_tuples_decompressed_per_dml_transaction = 0")
    db.execute(
        f"UPDATE {SCHEMA}.readings SET value = -1 WHERE ts = (SELECT min(ts) + interval '1 day' FROM {SCHEMA}.readings) AND channel = 'c1'")
    db.execute(
        f"DELETE FROM {SCHEMA}.readings WHERE ts = (SELECT min(ts) + interval '2 days' FROM {SCHEMA}.readings) AND channel = 'c2'")
    db.execute("RESET timescaledb.max_tuples_decompressed_per_dml_transaction")
    v = adopt.verify(db, _contract())
    assert not v.ok and v.missing_rows == 2 and len(v.failed_windows) == 2
    with pytest.raises(adopt.AdoptionRefused):
        adopt.drop_old(db, _contract())
    repaired = adopt.verify(db, _contract(), repair=True)
    assert repaired.ok and repaired.repaired_rows == 2
    assert adopt.verify(db, _contract()).ok
    assert _count(db) == plain
    adopt.drop_old(db, _contract())


def test_rows_written_after_the_swap_do_not_fail_verification(timescale_dsn, db, plain):
    """Negative control: the new table legitimately has MORE than the old one
    once the writer has moved to it, and that is not a loss."""
    adopt.run(db, _contract())
    adopt.swap(db, _contract())
    db.execute(f"INSERT INTO {SCHEMA}.readings VALUES ('a', 'f', 'late', now() - interval '3 days', 9.0)")
    v = adopt.verify(db, _contract())
    assert v.ok and v.missing_rows == 0 and v.extra_rows >= 1


def test_rollback_before_the_swap_removes_the_copy(db, plain):
    adopt.run(db, _contract(), max_windows=2)
    steps = adopt.rollback(db, _contract())
    assert db.execute(f"SELECT to_regclass('{SCHEMA}.readings__adopt')").fetchone()[0] is None
    assert not status(db, _contract()).is_hypertable and _count(db) == plain
    assert adopt.progress(db, _contract()).phase == "rolled_back"
    assert steps


def test_rollback_after_the_swap_restores_the_plain_table_and_its_views(db, plain):
    adopt.run(db, _contract())
    adopt.swap(db, _contract())
    db.execute(f"INSERT INTO {SCHEMA}.readings VALUES ('a', 'f', 'after', now(), 2.0)")
    adopt.rollback(db, _contract())
    assert not status(db, _contract()).is_hypertable
    assert db.execute(f"SELECT to_regclass('{SCHEMA}.readings__old')").fetchone()[0] is None
    assert db.execute(f"SELECT to_regclass('{SCHEMA}.readings__adopt')").fetchone()[0] is None
    assert "readings__" not in db.execute(f"SELECT pg_get_viewdef('{SCHEMA}.latest'::regclass)").fetchone()[0]
    # A row written to the hypertable between swap and rollback is carried back.
    assert _count(db) == plain + 1


def test_progress_is_readable_as_health(db, plain):
    adopt.run(db, _contract(), max_windows=4)
    p = adopt.progress(db, _contract())
    d = p.as_dict()
    assert d["phase"] == "copying" and d["windows_done"] == 4
    assert d["rows_copied"] > 0 and d["eta_seconds"] is not None and d["percent"] > 0
    assert adopt.progress(db, _contract(table="never")).phase == "none"


@pytest.mark.skipif(not os.environ.get("AXIOM_TSDB_SCALE_ROWS"), reason="opt-in: AXIOM_TSDB_SCALE_ROWS=30000000")
def test_tens_of_millions_of_rows_adopt_and_verify(db, capsys):
    rows = int(os.environ["AXIOM_TSDB_SCALE_ROWS"])
    _ledger(db)
    t0 = time.monotonic()
    n = _create_plain(db, rows)
    filled = time.monotonic() - t0
    est = adopt.estimate(db, _contract(chunk_interval=D), sample_rows=500_000)
    t1 = time.monotonic()
    p = adopt.run(db, _contract(chunk_interval=D))
    copied = time.monotonic() - t1
    t2 = time.monotonic()
    adopt.swap(db, _contract(chunk_interval=D))
    swapped = time.monotonic() - t2
    t3 = time.monotonic()
    v = adopt.verify(db, _contract(chunk_interval=D))
    verified = time.monotonic() - t3
    st = status(db, _contract(chunk_interval=D))
    old_bytes = db.execute(f"SELECT pg_total_relation_size('{SCHEMA}.readings__old')").fetchone()[0]
    with capsys.disabled():
        print(
            f"\nSCALE rows={n:,} fill={filled:.0f}s estimate: ratio={est.ratio:.1f}x "
            f"eta={est.eta_seconds:.0f}s windows={est.windows} | copy={copied:.0f}s "
            f"({p.rows_copied / copied:,.0f} rows/s) swap={swapped:.2f}s verify={verified:.0f}s | "
            f"old={old_bytes / 2**30:.2f} GiB new={st.total_bytes / 2**30:.3f} GiB "
            f"({old_bytes / max(st.total_bytes, 1):.1f}x)"
        )
    assert v.ok and p.failed_window is None and _count(db) == n
