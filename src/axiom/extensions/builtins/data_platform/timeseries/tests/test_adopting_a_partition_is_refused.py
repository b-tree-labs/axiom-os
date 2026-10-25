# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A table that is a partition is never adopted on its own (ADR-189 D5).

What happened on a node, 2026-10-20: the DEFAULT partition of a natively
partitioned parent was adopted by itself, and every reader and writer goes
through the parent. The adoption copied the partition's 985 million rows into
``<partition>__adopt``; ``--swap`` renamed the partition to ``__old`` and the
new hypertable to the partition's name, held the lock for 62 s, and printed
"next: verify, then drop_old". The new hypertable was not attached to the
parent (a hypertable cannot be a partition), so the parent's DEFAULT partition
was still the renamed old table: nothing read or wrote the copy, and the tool
said nothing.

So: ``estimate``, ``apply`` and ``swap`` refuse a partition and name the parent
and the command to run instead; and ``swap`` checks, before it commits, that a
read through the name (and through the parent) lands on the new table, and
rolls back inside its own transaction when it does not.

Against a real TimescaleDB (``af-*`` container; never the developer's database).
"""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from axiom.extensions.builtins.data_platform.timeseries import adopt
from axiom.extensions.builtins.data_platform.timeseries.contract import TimeseriesContract, status

from .conftest import SCHEMA

PARENT, DEFAULT, RANGE = "rt", "rt_default", "rt_recent"
CHANNELS = ("c1", "c2", "c3")
WINDOW = timedelta(hours=12)


def _contract(table: str = DEFAULT, **over) -> TimeseriesContract:
    base = dict(schema=SCHEMA, table=table, identity=("site", "feed", "channel"), chunk_interval=WINDOW)
    base.update(over)
    return TimeseriesContract(**base)


def _ledger(db) -> None:
    db.execute("DROP TABLE IF EXISTS data_platform.timeseries_adoption_windows")
    db.execute("DROP TABLE IF EXISTS data_platform.timeseries_adoptions")


def make_partitioned(db, rows_per_channel: int = 20_000) -> int:
    """A natively partitioned parent shaped like the one that failed: a DEFAULT
    partition holding the history, one range partition holding the recent days,
    a view and a role grant on the PARENT. Rows end two hours ago."""
    _ledger(db)
    db.execute(
        f"CREATE TABLE {SCHEMA}.{PARENT} (site text NOT NULL, feed text NOT NULL, channel text NOT NULL, "
        f"ts timestamptz NOT NULL, value double precision, UNIQUE (site, feed, channel, ts)) PARTITION BY RANGE (ts)"
    )
    db.execute(f"CREATE TABLE {SCHEMA}.{DEFAULT} PARTITION OF {SCHEMA}.{PARENT} DEFAULT")
    recent_from = (datetime.now(UTC) - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    db.execute(
        f"CREATE TABLE {SCHEMA}.{RANGE} PARTITION OF {SCHEMA}.{PARENT} "
        f"FOR VALUES FROM ('{recent_from.isoformat()}') TO ('{(recent_from + timedelta(days=4)).isoformat()}')"
    )
    end = datetime.now(UTC) - timedelta(hours=2)
    # 30 s apart: ~ rows_per_channel * 30 s of history per channel (7 days at 20k)
    db.execute(
        f"INSERT INTO {SCHEMA}.{PARENT} SELECT 'a', 'f', ch, %s - make_interval(secs => i * 30), "
        f"20.0 + (i %% 50) * 0.01 FROM generate_series(0, %s) i, unnest(%s::text[]) ch",
        (end, rows_per_channel - 1, list(CHANNELS)),
    )
    db.execute(f"CREATE INDEX {PARENT}_ts ON {SCHEMA}.{PARENT} (ts)")
    db.execute(f"ANALYZE {SCHEMA}.{PARENT}")
    db.execute(
        f"CREATE VIEW {SCHEMA}.rt_latest AS SELECT channel, max(ts) AS ts FROM {SCHEMA}.{PARENT} GROUP BY channel"
    )
    db.execute("DROP ROLE IF EXISTS ts_reader")
    db.execute("CREATE ROLE ts_reader")
    db.execute(f"GRANT SELECT ON {SCHEMA}.{PARENT} TO ts_reader")
    return rows_per_channel * len(CHANNELS)


def _in(db, table: str, where: str = "true") -> int:
    return db.execute(f"SELECT count(*) FROM {SCHEMA}.{table} WHERE {where}").fetchone()[0]


class ParentWriter(threading.Thread):
    """A live writer through the PARENT, as the real one writes."""

    def __init__(self, dsn: str, table: str = PARENT) -> None:
        super().__init__(daemon=True)
        self.dsn, self.table, self.rows, self.errors = dsn, table, 0, []
        self._halt = threading.Event()

    def run(self) -> None:
        with psycopg.connect(self.dsn, autocommit=True) as c:
            while not self._halt.is_set():
                try:
                    c.execute(f"INSERT INTO {SCHEMA}.{self.table} VALUES ('a', 'f', 'live', now(), 1.0) "
                              "ON CONFLICT DO NOTHING")
                    self.rows += 1
                except Exception as exc:  # noqa: BLE001 - the test reads them
                    self.errors.append(repr(exc))
                time.sleep(0.005)

    def stop(self) -> None:
        self._halt.set()
        self.join(timeout=10)


@pytest.fixture
def partitioned(db):
    return make_partitioned(db)


# -- the guard ----------------------------------------------------------------


def test_the_estimate_refuses_a_partition_and_names_the_parent_and_the_command(db, partitioned):
    est = adopt.estimate(db, _contract(), sample_rows=1_000)
    assert not est.feasible
    text = " ".join(est.blockers)
    assert f"{SCHEMA}.{PARENT}" in text and "partition" in text
    assert f"axi data adopt-timeseries {SCHEMA}.{PARENT}" in text, "the exact command, not a hint"
    assert "--identity site,feed,channel" in text, "the contract's identity travels into the command"
    assert db.execute(f"SELECT to_regclass('{SCHEMA}.{DEFAULT}__adopt')").fetchone()[0] is None


def test_apply_refuses_a_partition_before_creating_anything(db, partitioned):
    with pytest.raises(adopt.PartitionRefused) as e:
        adopt.run(db, _contract())
    assert f"{SCHEMA}.{PARENT}" in str(e.value) and f"axi data adopt-timeseries {SCHEMA}.{PARENT}" in str(e.value)
    assert db.execute(f"SELECT to_regclass('{SCHEMA}.{DEFAULT}__adopt')").fetchone()[0] is None
    assert adopt.progress(db, _contract()).phase == "none"


def test_a_range_partition_is_refused_too_and_a_subpartition_names_the_root(db, partitioned):
    with pytest.raises(adopt.PartitionRefused):
        adopt.run(db, _contract(RANGE))
    db.execute(
        f"CREATE TABLE {SCHEMA}.rt_mid PARTITION OF {SCHEMA}.{PARENT} FOR VALUES FROM ('2001-01-01') TO ('2002-01-01') "
        "PARTITION BY RANGE (ts)"
    )
    est = adopt.estimate(db, _contract("rt_mid"))
    assert not est.feasible and f"{SCHEMA}.{PARENT}" in " ".join(est.blockers), "the ROOT, not the middle"


def test_a_copy_already_begun_on_a_partition_stops_at_the_next_apply_and_swap(db, partitioned):
    """The ledger may predate the guard (the state a node was left in): the guard is checked
    on every call, not only at the start."""
    c = _contract()
    # A copy begun the way the unguarded code began it.
    adopt._ensure_ledger(db)
    db.execute(f"CREATE TABLE {SCHEMA}.{DEFAULT}__adopt (LIKE {SCHEMA}.{DEFAULT} INCLUDING ALL)")
    db.execute(
        f"INSERT INTO {adopt.ADOPTIONS} (qualified, phase, window_seconds, range_lo, range_hi, boundary) "
        "VALUES (%s, 'copying', 43200, now() - interval '9 days', now(), now())", (c.qualified,))
    with pytest.raises(adopt.PartitionRefused):
        adopt.run(db, c)
    with pytest.raises(adopt.PartitionRefused):
        adopt.swap(db, c)
    # Rollback is never refused: it is how the operator leaves.
    adopt.rollback(db, c)
    assert db.execute(f"SELECT to_regclass('{SCHEMA}.{DEFAULT}__adopt')").fetchone()[0] is None


def test_an_inheritance_child_is_refused_like_a_partition(db):
    _ledger(db)
    db.execute(f"CREATE TABLE {SCHEMA}.base (site text, feed text, channel text, ts timestamptz NOT NULL, value float8)")
    db.execute(f"CREATE TABLE {SCHEMA}.kid () INHERITS ({SCHEMA}.base)")
    db.execute(f"CREATE INDEX kid_ts ON {SCHEMA}.kid (ts)")
    db.execute(f"INSERT INTO {SCHEMA}.kid VALUES ('a','f','c',now() - interval '3 hours', 1)")
    est = adopt.estimate(db, _contract("kid"))
    assert not est.feasible and f"{SCHEMA}.base" in " ".join(est.blockers)


# -- the post-swap check ------------------------------------------------------


def test_a_swap_that_would_leave_readers_on_the_old_table_rolls_back_and_says_so(db, partitioned, monkeypatch):
    """The guard defeated on purpose (as it was not, on the node that failed): the swap's own
    check still sees that a read through the parent lands on the old table,
    rolls the whole swap back in its transaction, and prints no 'next' line."""
    c = _contract()
    monkeypatch.setattr(adopt, "partition_refusal", lambda *a, **k: None)
    adopt.run(db, c)
    with pytest.raises(adopt.SwapUnsafe) as e:
        adopt.swap(db, c)
    assert f"{SCHEMA}.{PARENT}" in str(e.value) and "old table" in str(e.value)
    # Rolled back: the names are where they were, the partition is still the partition.
    assert db.execute(f"SELECT to_regclass('{SCHEMA}.{DEFAULT}__old')").fetchone()[0] is None
    assert not status(db, c).is_hypertable
    assert db.execute(
        "SELECT count(*) FROM pg_inherits i JOIN pg_class p ON p.oid = i.inhparent "
        "JOIN pg_class k ON k.oid = i.inhrelid WHERE p.relname = %s AND k.relname = %s", (PARENT, DEFAULT)
    ).fetchone()[0] == 1
    assert adopt.progress(db, c).phase == "copying", "the ledger did not advance"
    assert _in(db, PARENT) == partitioned


def test_a_swap_already_done_on_a_partition_is_undone_by_rollback_and_the_watch_names_it(db, partitioned, monkeypatch):
    """The state a node was left in: swapped, the parent still reading the old
    partition. The watch raises it; rollback puts the partition back."""
    from axiom.extensions.builtins.data_platform.timeseries import adopt_watch

    c = _contract()
    monkeypatch.setattr(adopt, "partition_refusal", lambda *a, **k: None)
    monkeypatch.setattr(adopt, "check_reads_land_on_new", lambda *a, **k: ["(check defeated)"])
    adopt.run(db, c)
    adopt.swap(db, c)
    assert status(db, c).is_hypertable and _in(db, DEFAULT + "__old") > 0

    # the watch's own read of the database, through the connection under test
    conn_src = adopt_watch.PgSource(contract_for=lambda j: c, connect=lambda *a, **k: _Keep(db))
    view = conn_src.ledger(adopt_watch.Job(table=c.qualified))
    assert view.stranded and f"{SCHEMA}.{PARENT}" in view.stranded
    faults = adopt_watch.evaluate([adopt_watch.Job(table=c.qualified)], type("S", (), {
        "ledger": lambda self, job: view, "service": lambda self, job: adopt_watch.ServiceView("active")})(),
        datetime.now(UTC))
    assert [f.kind for f in faults] == [adopt_watch.PARTITION] and "--rollback" in faults[0].message

    adopt.rollback(db, c)
    assert not status(db, c).is_hypertable
    assert db.execute(f"SELECT to_regclass('{SCHEMA}.{DEFAULT}__old')").fetchone()[0] is None
    assert _in(db, DEFAULT) > 0 and _in(db, PARENT) == partitioned


class _Keep:
    """A connection the caller owns, usable in ``with`` without being closed."""

    def __init__(self, conn) -> None:
        self._c = conn

    def __enter__(self):
        return self._c

    def __exit__(self, *a):
        return False


def test_the_unattended_job_refuses_the_same_and_halts_loudly_instead_of_finishing(timescale_dsn, db, partitioned, tmp_path):
    from axiom.extensions.builtins.data_platform.timeseries import adopt_service as svc
    from axiom.extensions.builtins.data_platform.timeseries import adopt_watch

    c = _contract()
    store = svc.JobStore(tmp_path / "state")
    job = svc.schedule(tmp_path / "state", c.qualified, params={"dsn": timescale_dsn},
                       change_ledger=tmp_path / "changes.jsonl")
    out = svc.run_tick(job, c, store, change_ledger=tmp_path / "changes.jsonl")
    assert out.status == "halted" and f"{SCHEMA}.{PARENT}" in out.message
    again = store.get(c.qualified)
    assert again.state == "halted" and again.halted_kind == "partition", "not 'done': nobody is told a refusal is a finish"
    assert db.execute(f"SELECT to_regclass('{SCHEMA}.{DEFAULT}__adopt')").fetchone()[0] is None
    # and the watch says it, in its own words, latched
    class _Src:
        def ledger(self, job):
            return None

        def service(self, job):
            return adopt_watch.ServiceView("active", datetime.now(UTC))

    faults = adopt_watch.evaluate([again], _Src(), datetime.now(UTC))
    assert [f.kind for f in faults] == [adopt_watch.PARTITION] and faults[0].latched
    assert f"{SCHEMA}.{PARENT}" in faults[0].message


def test_the_verb_refuses_every_step_on_a_partition_and_never_says_next(timescale_dsn, db, partitioned, tmp_path):
    import logging

    from axiom.extensions.builtins.data_platform.skills import adopt_timeseries as skill
    from axiom.infra.skills import SkillContext, default_registry

    ctx = SkillContext(registry=default_registry(), state_dir=tmp_path, logger=logging.getLogger("t"), user_prompt=None)
    base = {"table": f"{SCHEMA}.{DEFAULT}", "identity": "site,feed,channel", "dsn": timescale_dsn,
            "chunk_interval": "12h"}
    command = f"axi data adopt-timeseries {SCHEMA}.{PARENT}"
    for step in ({}, {"apply": True}, {"swap": True}, {"schedule": True}):
        r = skill.run({**base, **step}, ctx)
        assert not r.ok, step
        assert command in " ".join(r.errors), (step, r.errors)
        assert not any("next:" in a for a in r.actions_taken), r.actions_taken
