# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""An unattended adoption copy survives being killed, being cut off, and finishing.

Against a real TimescaleDB in an ``adopt-<hex>`` container (never the
developer's database; removed by name, nothing pruned). The properties:

- a ``kill -9`` in the middle of a window loses only that window, the lock it
  held goes with its connection, and the next fire resumes and finishes with
  **no duplicate and no missing row**: count and an order-independent hash of
  the copy equal the source's;
- a connection dropped mid-window is reconnected and the batch carries on;
- when every window is copied and verified the job marks itself done and does
  **not** swap, verify or drop anything;
- a second process cannot copy the same table at the same time.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import textwrap
import time
from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from axiom.extensions.builtins.data_platform.tests import timescale_container
from axiom.extensions.builtins.data_platform.timeseries import adopt
from axiom.extensions.builtins.data_platform.timeseries import adopt_service as svc
from axiom.extensions.builtins.data_platform.timeseries.contract import TimeseriesContract

SCHEMA = "adopt_svc"
TABLE = "readings"
WINDOW = "12h"
HASH = ("SELECT count(*), coalesce(sum(('x' || substr(md5(t::text), 1, 16))::bit(64)::bigint), 0) "
        "FROM {rel} t WHERE ts < %s")


@pytest.fixture(scope="module")
def dsn():
    os.environ[timescale_container.PREFIX_ENV] = "adopt"
    try:
        name, url = timescale_container.start()
    finally:
        os.environ.pop(timescale_container.PREFIX_ENV, None)
    assert name.startswith("adopt-")
    yield url
    timescale_container.stop(name)


def _contract() -> TimeseriesContract:
    return TimeseriesContract(schema=SCHEMA, table=TABLE, identity=("site", "channel"),
                              chunk_interval=timedelta(hours=12))


@pytest.fixture
def db(dsn):
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")
        c.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        c.execute("DROP TABLE IF EXISTS data_platform.timeseries_adoption_windows")
        c.execute("DROP TABLE IF EXISTS data_platform.timeseries_adoptions")
        c.execute(f"CREATE SCHEMA {SCHEMA}")
        c.execute(f"CREATE TABLE {SCHEMA}.{TABLE} (site text NOT NULL, channel text NOT NULL, "
                  "ts timestamptz NOT NULL, value double precision, UNIQUE (site, channel, ts))")
        c.execute(f"CREATE INDEX ON {SCHEMA}.{TABLE} (ts)")
        end = datetime.now(UTC) - timedelta(hours=3)
        # ~5 days, one reading a minute on three channels: ten windows of 12 h.
        c.execute(
            f"INSERT INTO {SCHEMA}.{TABLE} SELECT 'a', ch, %s - make_interval(mins => i), 20 + (i %% 50) * 0.01 "
            "FROM generate_series(0, 7199) i, unnest(%s::text[]) ch", (end, ["c1", "c2", "c3"]))
        c.execute(f"ANALYZE {SCHEMA}.{TABLE}")
        yield c


@pytest.fixture(autouse=True)
def _ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("AXI_CHANGE_LEDGER", str(tmp_path / "changes.jsonl"))


def _job(dsn, tmp_path, **over) -> tuple[svc.Job, svc.JobStore]:
    st = svc.JobStore(tmp_path / "state")
    job = svc.schedule(tmp_path / "state", f"{SCHEMA}.{TABLE}",
                       params={"identity": "site,channel", "window": WINDOW, "dsn": dsn},
                       every_seconds=60, batch_seconds=over.pop("batch_seconds", 600),
                       ready_timeout_seconds=20, change_ledger=tmp_path / "changes.jsonl")
    for k, v in over.items():
        setattr(job, k, v)
    st.put(job)
    return job, st


def _same_rows(db) -> tuple[tuple, tuple]:
    """Count and order-independent hash of every row before the copy's boundary
    (what arrived after it is the swap's tail, copied under its short lock)."""
    boundary = db.execute("SELECT boundary FROM data_platform.timeseries_adoptions").fetchone()[0]
    src = db.execute(HASH.format(rel=f"{SCHEMA}.{TABLE}"), (boundary,)).fetchone()
    new = db.execute(HASH.format(rel=f'{SCHEMA}."{TABLE}__adopt"'), (boundary,)).fetchone()
    return tuple(src), tuple(new)


def _windows(db) -> int:
    return db.execute("SELECT count(*) FROM data_platform.timeseries_adoption_windows").fetchone()[0]


DRIVER = textwrap.dedent('''
    import sys, time, pathlib
    from axiom.extensions.builtins.data_platform.timeseries import adopt_service as svc
    from axiom.extensions.builtins.data_platform.timeseries.contract import TimeseriesContract
    state, table, marker, stop_at = sys.argv[1], sys.argv[2], pathlib.Path(sys.argv[3]), int(sys.argv[4])
    st = svc.JobStore(state)
    job = st.get(table)
    schema, _, name = table.partition(".")
    contract = TimeseriesContract(schema=schema, table=name, identity=("site", "channel"))
    seen = {"n": 0}
    def after_copy(conn, c, lo, hi, n):
        seen["n"] += 1
        if seen["n"] == stop_at:
            marker.write_text("inside the window's transaction")
            time.sleep(600)
    svc.run_tick(job, contract, st, after_copy=after_copy)
''')


def test_a_kill_9_in_the_middle_of_a_window_loses_nothing_and_duplicates_nothing(db, dsn, tmp_path):
    job, st = _job(dsn, tmp_path)
    marker = tmp_path / "marker"
    proc = subprocess.Popen([sys.executable, "-c", DRIVER, str(tmp_path / "state"), job.table,
                             str(marker), "3"], env={**os.environ})
    try:
        deadline = time.monotonic() + 120
        while not marker.exists():
            assert proc.poll() is None, "the copy ended before it reached the window it was to die in"
            assert time.monotonic() < deadline, "the copy never reached the third window"
            time.sleep(0.2)
        # The third window has been inserted but not committed; the process holds the table's lock.
        assert _windows(db) == 2
        with psycopg.connect(dsn, autocommit=True) as other:
            with pytest.raises(adopt.AdoptionBusy):
                adopt.run(other, _contract(), max_windows=1)
        os.kill(proc.pid, signal.SIGKILL)
        proc.wait(timeout=30)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert proc.returncode == -signal.SIGKILL
    assert _windows(db) == 2, "the killed window was rolled back with its connection"
    assert db.execute(f'SELECT count(*) FROM {SCHEMA}."{TABLE}__adopt"').fetchone()[0] == \
        db.execute("SELECT sum(rows) FROM data_platform.timeseries_adoption_windows").fetchone()[0], \
        "no half-copied window is in the new table"

    # The next fire needs nothing from the dead one: not its lock, not a repair step.
    job = st.get(job.table)
    out = svc.run_tick(job, _contract(), st, change_ledger=tmp_path / "changes.jsonl")
    assert out.status == "done", out.message
    src, new = _same_rows(db)
    assert src == new, "the copy has exactly the source's rows: count and hash"
    assert src[0] > 15_000, "the comparison covered most of the table"
    assert db.execute(
        f'SELECT count(*) - count(DISTINCT (site, channel, ts)) FROM {SCHEMA}."{TABLE}__adopt"').fetchone()[0] == 0


def test_a_dropped_connection_is_reconnected_and_the_batch_carries_on(db, dsn, tmp_path):
    job, st = _job(dsn, tmp_path)
    calls = {"n": 0}

    def cut_the_third_window_off(conn, c, lo, hi, n):
        calls["n"] += 1
        if calls["n"] == 3:
            with psycopg.connect(dsn, autocommit=True) as killer:
                killer.execute("SELECT pg_terminate_backend(%s)", (conn.info.backend_pid,))

    out = svc.run_tick(job, _contract(), st, after_copy=cut_the_third_window_off,
                       change_ledger=tmp_path / "changes.jsonl")
    assert out.reconnects == 1, out
    assert out.status == "done", out.message
    src, new = _same_rows(db)
    assert src == new and src[0] > 15_000


def test_a_finished_copy_marks_the_job_done_and_does_not_swap_or_drop(db, dsn, tmp_path):
    job, st = _job(dsn, tmp_path)
    out = svc.run_tick(job, _contract(), st, change_ledger=tmp_path / "changes.jsonl")
    assert out.status == "done" and out.ok
    assert st.get(job.table).state == "done" and st.get(job.table).finished_at
    p = adopt.progress(db, _contract())
    assert p.phase == "copying" and p.windows_done == p.windows_total, "copied and verified, not swapped"
    names = {r[0] for r in db.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = %s", (SCHEMA,))}
    assert names >= {TABLE, f"{TABLE}__adopt"} and f"{TABLE}__old" not in names
    assert not db.execute("SELECT 1 FROM timescaledb_information.hypertables WHERE hypertable_schema = %s "
                          "AND hypertable_name = %s", (SCHEMA, TABLE)).fetchone(), "the live table is untouched"
    again = svc.run_tick(st.get(job.table), _contract(), st)
    assert again.status == "idle", "a done job does nothing on any later fire"

    kinds = [(json.loads(x)["phase"], json.loads(x).get("outcome")) for x in
             (tmp_path / "changes.jsonl").read_text().splitlines()]
    assert ("outcome", "started") in kinds and ("outcome", "copied") in kinds, "ADR-182 records bracket the copy"
    assert kinds.index(("outcome", "started")) < kinds.index(("outcome", "copied"))


def test_a_batch_stops_at_its_budget_and_the_next_fire_continues(db, dsn, tmp_path):
    job, st = _job(dsn, tmp_path, batch_seconds=1)
    ticks = iter(range(0, 1000, 2))  # every look at the clock is two seconds later
    out = svc.run_tick(job, _contract(), st, clock=lambda: float(next(ticks)))
    assert out.status == "progress" and 1 <= _windows(db) < 10, out
    left = _windows(db)
    out = svc.run_tick(st.get(job.table), _contract(), st, batch_seconds=None) if False else \
        svc.run_tick(st.get(job.table), _contract(), st, clock=lambda: 0.0)
    assert out.status == "done" and _windows(db) > left
    src, new = _same_rows(db)
    assert src == new


def test_a_window_that_will_not_verify_halts_the_job_and_is_kept_in_the_ledger(db, dsn, tmp_path):
    job, st = _job(dsn, tmp_path)

    def corrupt(conn, c, lo, hi, n):
        conn.execute(
            f'DELETE FROM {SCHEMA}."{TABLE}__adopt" WHERE ts = (SELECT min(ts) FROM {SCHEMA}."{TABLE}__adopt" '
            "WHERE ts >= %s AND ts < %s)", (lo, hi))

    out = svc.run_tick(job, _contract(), st, after_copy=corrupt)
    assert out.status == "halted" and not out.ok
    assert st.get(job.table).state == "halted" and "did not verify" in st.get(job.table).halted_reason
    note = db.execute("SELECT notes->'last_failure' FROM data_platform.timeseries_adoptions").fetchone()[0]
    assert note["message"].startswith("row count differs") and note["window_start"]
    assert svc.run_tick(st.get(job.table), _contract(), st).status == "idle", "a halted job does not hammer"
