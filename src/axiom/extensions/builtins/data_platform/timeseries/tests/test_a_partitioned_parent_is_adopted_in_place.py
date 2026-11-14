# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A natively partitioned PARENT is adopted whole, end to end (ADR-189 D5).

The right target for a table that is a partition is its parent: the hypertable
is created under the parent's name from every partition's rows (the DEFAULT
partition included, partition bounds irrelevant), the parent is renamed to
``__old`` with its partitions still attached to it, and the readers and the
writer, who go through the parent's name, land on the hypertable.

Against a real TimescaleDB (``af-*`` container; never the developer's database).
"""

from __future__ import annotations

import time
from datetime import timedelta

import pytest

from axiom.extensions.builtins.data_platform.timeseries import adopt
from axiom.extensions.builtins.data_platform.timeseries.contract import TimeseriesContract, status

from .conftest import SCHEMA
from .test_adopting_a_partition_is_refused import (
    DEFAULT,
    PARENT,
    RANGE,
    ParentWriter,
    _in,
    make_partitioned,
)

WINDOW = timedelta(hours=12)


def _c(**over) -> TimeseriesContract:
    base = dict(schema=SCHEMA, table=PARENT, identity=("site", "feed", "channel"), chunk_interval=WINDOW)
    base.update(over)
    return TimeseriesContract(**base)


@pytest.fixture
def parent(db):
    """The partitioned parent of the failure, plus what the swap must carry:
    a serial column, a column default, comments, an owner, a grant, a view."""
    n = make_partitioned(db)
    db.execute(f"ALTER TABLE {SCHEMA}.{PARENT} ADD COLUMN n bigserial")
    db.execute(f"ALTER TABLE {SCHEMA}.{PARENT} ALTER COLUMN feed SET DEFAULT 'dflt'")
    db.execute(f"COMMENT ON TABLE {SCHEMA}.{PARENT} IS 'the history'")
    db.execute(f"COMMENT ON COLUMN {SCHEMA}.{PARENT}.value IS 'a reading'")
    db.execute("DROP OWNED BY ts_owner") if db.execute("SELECT 1 FROM pg_roles WHERE rolname = 'ts_owner'").fetchone() else None
    db.execute("DROP ROLE IF EXISTS ts_owner")
    db.execute("CREATE ROLE ts_owner")
    db.execute(f"GRANT USAGE, CREATE ON SCHEMA {SCHEMA} TO ts_owner")
    db.execute(f"ALTER TABLE {SCHEMA}.{PARENT} OWNER TO ts_owner")
    db.execute(f"ALTER SEQUENCE {SCHEMA}.{PARENT}_n_seq OWNER TO ts_owner")
    db.execute(f"GRANT SELECT ON {SCHEMA}.{PARENT} TO ts_reader")
    return n


def _children(db, parent: str) -> set[str]:
    return {r[0] for r in db.execute(
        "SELECT k.relname FROM pg_inherits i JOIN pg_class k ON k.oid = i.inhrelid "
        "JOIN pg_class p ON p.oid = i.inhparent JOIN pg_namespace n ON n.oid = p.relnamespace "
        "WHERE n.nspname = %s AND p.relname = %s", (SCHEMA, parent)).fetchall()}


def _owner(db, rel: str) -> str:
    return db.execute(
        "SELECT pg_get_userbyid(relowner) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = %s AND c.relname = %s", (SCHEMA, rel)).fetchone()[0]


# -- estimate -----------------------------------------------------------------


def test_the_estimate_counts_every_partition_and_says_the_parent_swaps_out(db, parent):
    est = adopt.estimate(db, _c(), sample_rows=20_000)
    assert est.feasible, est.blockers
    # A planner estimate, never a count: pg_stat's n_live_tup can run ahead of the
    # rows for a while (twice, on a loaded machine). What must hold is that every
    # partition is in it: the default partition alone is three quarters of the rows.
    assert parent * 0.95 <= est.rows <= parent * 2.1, "the default partition and the range partition, together"
    assert any("2 partition(s)" in n for n in est.notes), est.notes
    assert est.windows >= 10 and est.window == WINDOW
    assert est.dependent_views == [f"{SCHEMA}.rt_latest"]
    assert db.execute(f"SELECT to_regclass('{SCHEMA}.{PARENT}__adopt')").fetchone()[0] is None


def test_a_parent_whose_partitions_carry_the_time_index_has_one(db, parent):
    """The index may live on the partitions only (the parent's is optional)."""
    db.execute(f"DROP INDEX {SCHEMA}.{PARENT}_ts")
    db.execute(f"CREATE INDEX ON {SCHEMA}.{DEFAULT} (ts)")
    db.execute(f"CREATE INDEX ON {SCHEMA}.{RANGE} (ts)")
    f = adopt.source_facts(db, _c())
    assert f.time_index and f.time_btree
    # one partition without it: no time index, and the blocker says which
    db.execute(f"DROP INDEX {SCHEMA}.{DEFAULT}_ts_idx")
    est = adopt.estimate(db, _c())
    assert not est.feasible and "brin" in " ".join(est.blockers).lower()


# -- the whole path, with a writer through the parent --------------------------


def test_adoption_of_the_parent_end_to_end_with_a_live_writer(timescale_dsn, db, parent):
    c = _c()
    writer = ParentWriter(timescale_dsn)
    writer.start()
    time.sleep(0.2)
    try:
        adopt.run(db, c)
        steps = adopt.swap(db, c)
        time.sleep(0.3)
    finally:
        writer.stop()
    assert not writer.errors, writer.errors[:3]
    assert writer.rows > 20

    # The parent's name is the hypertable; the old parent kept its partitions.
    assert status(db, c).is_hypertable
    assert _in(db, PARENT) == parent + writer.rows, "every partition's rows plus every row written during the adoption"
    assert db.execute(f"SELECT relkind FROM pg_class WHERE oid = '{SCHEMA}.{PARENT}__old'::regclass").fetchone()[0] == "p"
    assert _children(db, PARENT + "__old") == {DEFAULT, RANGE}
    assert _children(db, PARENT) and not _children(db, PARENT) & {DEFAULT, RANGE}, "chunks, not partitions"
    assert any("post-swap check passed" in s for s in steps), steps

    # What the swap carries.
    assert "rt__old" not in db.execute(f"SELECT pg_get_viewdef('{SCHEMA}.rt_latest'::regclass)").fetchone()[0]
    assert db.execute(f"SELECT count(*) FROM {SCHEMA}.rt_latest").fetchone()[0] == 4  # c1..c3 + live
    assert db.execute("SELECT has_table_privilege('ts_reader', %s, 'SELECT')", (f"{SCHEMA}.{PARENT}",)).fetchone()[0]
    assert _owner(db, PARENT) == "ts_owner"
    assert db.execute(f"SELECT obj_description('{SCHEMA}.{PARENT}'::regclass, 'pg_class')").fetchone()[0] == "the history"
    assert db.execute(f"SELECT col_description('{SCHEMA}.{PARENT}'::regclass, "
                      "(SELECT attnum FROM pg_attribute WHERE attrelid = '%s.%s'::regclass AND attname = 'value'))"
                      % (SCHEMA, PARENT)).fetchone()[0] == "a reading"
    assert db.execute(f"SELECT pg_get_expr(adbin, adrelid) FROM pg_attrdef WHERE adrelid = '{SCHEMA}.{PARENT}'::regclass "
                      "AND adnum = (SELECT attnum FROM pg_attribute WHERE attrelid = '%s.%s'::regclass AND attname = 'feed')"
                      % (SCHEMA, PARENT)).fetchone()[0].startswith("'dflt'")
    # The serial column keeps counting, from the same sequence, now owned by the new table.
    top = db.execute(f"SELECT max(n) FROM {SCHEMA}.{PARENT}").fetchone()[0]
    db.execute(f"INSERT INTO {SCHEMA}.{PARENT} (site, channel, ts, value) VALUES ('a', 'after', now(), 3.0)")
    assert db.execute(f"SELECT n, feed FROM {SCHEMA}.{PARENT} WHERE channel = 'after'").fetchone() == (top + 1, "dflt")
    assert db.execute(f"SELECT pg_get_serial_sequence('{SCHEMA}.{PARENT}', 'n')").fetchone()[0] is not None


def test_verify_compares_across_every_partition_and_finds_a_row_lost_from_either(db, parent):
    c = _c()
    adopt.run(db, c)
    adopt.swap(db, c)
    v = adopt.verify(db, c)
    assert v.ok and v.windows_checked >= 10 and v.missing_rows == 0
    # A row from the DEFAULT partition's range and one from the range partition's: both gone from the copy.
    db.execute("SET timescaledb.max_tuples_decompressed_per_dml_transaction = 0")
    cut = db.execute(f"SELECT max(ts) FROM {SCHEMA}.{DEFAULT}").fetchone()[0]
    db.execute(f"DELETE FROM {SCHEMA}.{PARENT} WHERE channel = 'c1' AND ts = %s", (cut,))
    db.execute(f"DELETE FROM {SCHEMA}.{PARENT} WHERE channel = 'c2' AND ts = (SELECT min(ts) FROM {SCHEMA}.{RANGE} WHERE channel = 'c2')")
    db.execute("RESET timescaledb.max_tuples_decompressed_per_dml_transaction")
    lost = adopt.verify(db, c)
    assert not lost.ok and lost.missing_rows == 2
    with pytest.raises(adopt.AdoptionRefused):
        adopt.drop_old(db, c)
    assert adopt.verify(db, c, repair=True).repaired_rows == 2
    assert adopt.verify(db, c).ok


def test_drop_old_re_verifies_and_then_drops_the_old_parent_with_its_partitions(db, parent):
    c = _c()
    adopt.run(db, c)
    adopt.swap(db, c)
    assert adopt.verify(db, c).ok
    # Damage AFTER the verification passed: drop-old must see it, not trust the earlier pass.
    db.execute("SET timescaledb.max_tuples_decompressed_per_dml_transaction = 0")
    db.execute(f"DELETE FROM {SCHEMA}.{PARENT} WHERE channel = 'c3' AND ts = (SELECT min(ts) FROM {SCHEMA}.{PARENT} WHERE channel = 'c3')")
    db.execute("RESET timescaledb.max_tuples_decompressed_per_dml_transaction")
    with pytest.raises(adopt.AdoptionRefused) as e:
        adopt.drop_old(db, c)
    assert "re-verif" in str(e.value) or "missing" in str(e.value)
    assert _children(db, PARENT + "__old") == {DEFAULT, RANGE}, "nothing was dropped"
    adopt.verify(db, c, repair=True)
    steps = adopt.drop_old(db, c)
    for gone in (PARENT + "__old", DEFAULT, RANGE):
        assert db.execute(f"SELECT to_regclass('{SCHEMA}.{gone}')").fetchone()[0] is None, gone
    assert any("dropped" in s and "partition" in s for s in steps), steps
    assert _in(db, PARENT) == parent


def test_rollback_restores_the_exact_original_structure(timescale_dsn, db, parent):
    c = _c()
    before_children = _children(db, PARENT)
    bounds = db.execute(
        "SELECT c.relname, pg_get_expr(c.relpartbound, c.oid) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = %s AND c.relispartition ORDER BY 1", (SCHEMA,)).fetchall()
    adopt.run(db, c)
    adopt.swap(db, c)
    db.execute(f"INSERT INTO {SCHEMA}.{PARENT} (site, channel, ts, value) VALUES ('a', 'written-after-swap', now(), 2.0)")
    steps = adopt.rollback(db, c)
    assert steps
    assert not status(db, c).is_hypertable
    assert db.execute(f"SELECT relkind FROM pg_class WHERE oid = '{SCHEMA}.{PARENT}'::regclass").fetchone()[0] == "p"
    assert _children(db, PARENT) == before_children
    assert db.execute(
        "SELECT c.relname, pg_get_expr(c.relpartbound, c.oid) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = %s AND c.relispartition ORDER BY 1", (SCHEMA,)).fetchall() == bounds
    for gone in (PARENT + "__old", PARENT + "__adopt"):
        assert db.execute(f"SELECT to_regclass('{SCHEMA}.{gone}')").fetchone()[0] is None
    assert _in(db, PARENT) == parent + 1, "the row written after the swap came back"
    assert _owner(db, PARENT) == "ts_owner"
    assert "__" not in db.execute(f"SELECT pg_get_viewdef('{SCHEMA}.rt_latest'::regclass)").fetchone()[0]
    assert db.execute("SELECT has_table_privilege('ts_reader', %s, 'SELECT')", (f"{SCHEMA}.{PARENT}",)).fetchone()[0]
    # The serial keeps counting on the original again, past what the hypertable handed out.
    db.execute(f"INSERT INTO {SCHEMA}.{PARENT} (site, channel, ts, value) VALUES ('a', 'again', now(), 2.0)")
    ns = [r[0] for r in db.execute(f"SELECT n FROM {SCHEMA}.{PARENT} ORDER BY n").fetchall()]
    assert len(ns) == len(set(ns)), "no sequence value was handed out twice"
    assert adopt.progress(db, c).phase == "rolled_back"


def test_a_copy_that_resumes_after_a_partial_run_covers_every_partition(db, parent):
    c = _c()
    first = adopt.run(db, c, max_windows=3)
    assert first.phase == "copying" and first.windows_done == 3
    done = adopt.run(db, c)
    assert done.windows_done == done.windows_total
    adopt.swap(db, c)
    assert _in(db, PARENT) == parent


# -- sequences, on a plain table too -----------------------------------------


def _serial_table(db) -> None:
    db.execute("DROP TABLE IF EXISTS data_platform.timeseries_adoption_windows")
    db.execute("DROP TABLE IF EXISTS data_platform.timeseries_adoptions")
    db.execute(
        f"CREATE TABLE {SCHEMA}.ser (site text NOT NULL, feed text NOT NULL, channel text NOT NULL, ts timestamptz NOT NULL, "
        "value double precision, id bigserial, ident bigint GENERATED BY DEFAULT AS IDENTITY, "
        "UNIQUE (site, feed, channel, ts))")
    db.execute(f"CREATE INDEX ser_ts ON {SCHEMA}.ser (ts)")
    db.execute(
        f"INSERT INTO {SCHEMA}.ser (site, feed, channel, ts, value) SELECT 'a', 'f', 'c', now() - interval '3 hours' "
        "- make_interval(secs => i * 60), 1 FROM generate_series(0, 20000) i")
    db.execute(f"ANALYZE {SCHEMA}.ser")


def test_a_serial_and_an_identity_column_survive_the_swap_and_the_drop(db):
    """A serial's sequence belongs to the old table's column and a drop of the
    old table takes it, and every default that reads it, along: it must move."""
    _serial_table(db)
    c = _c(table="ser", identity=("site", "feed", "channel"))
    adopt.run(db, c)
    steps = adopt.swap(db, c)
    assert any("ser_id_seq now owned by" in s for s in steps), steps
    top_id, top_ident = db.execute(f"SELECT max(id), max(ident) FROM {SCHEMA}.ser").fetchone()
    db.execute(f"INSERT INTO {SCHEMA}.ser (site, feed, channel, ts, value) VALUES ('a', 'f', 'z', now(), 1)")
    assert db.execute(f"SELECT id, ident FROM {SCHEMA}.ser WHERE channel = 'z'").fetchone() == (top_id + 1, top_ident + 1)
    assert adopt.verify(db, c).ok
    adopt.drop_old(db, c)
    db.execute(f"INSERT INTO {SCHEMA}.ser (site, feed, channel, ts, value) VALUES ('a', 'f', 'y', now(), 1)")
    assert db.execute(f"SELECT id FROM {SCHEMA}.ser WHERE channel = 'y'").fetchone()[0] == top_id + 2


def test_rollback_hands_the_sequences_back_without_reusing_a_value(db):
    _serial_table(db)
    c = _c(table="ser", identity=("site", "feed", "channel"))
    adopt.run(db, c)
    adopt.swap(db, c)
    db.execute(f"INSERT INTO {SCHEMA}.ser (site, feed, channel, ts, value) VALUES ('a', 'f', 'z', now(), 1)")
    adopt.rollback(db, c)
    db.execute(f"INSERT INTO {SCHEMA}.ser (site, feed, channel, ts, value) VALUES ('a', 'f', 'y', now(), 1)")
    ids = [r[0] for r in db.execute(f"SELECT id FROM {SCHEMA}.ser").fetchall()]
    idents = [r[0] for r in db.execute(f"SELECT ident FROM {SCHEMA}.ser").fetchall()]
    assert len(ids) == len(set(ids)) and len(idents) == len(set(idents))


def test_the_unattended_job_copies_a_parent_and_stops_before_the_swap(timescale_dsn, db, parent, tmp_path):
    from axiom.extensions.builtins.data_platform.timeseries import adopt_service as svc

    c = _c()
    store = svc.JobStore(tmp_path / "state")
    job = svc.schedule(tmp_path / "state", c.qualified, params={"dsn": timescale_dsn},
                       change_ledger=tmp_path / "changes.jsonl")
    out = svc.run_tick(job, c, store, change_ledger=tmp_path / "changes.jsonl")
    assert out.status == "done", out.message
    assert not status(db, c).is_hypertable, "the swap is never scheduled"
    p = adopt.progress(db, c)
    assert p.phase == "copying" and p.windows_done == p.windows_total
