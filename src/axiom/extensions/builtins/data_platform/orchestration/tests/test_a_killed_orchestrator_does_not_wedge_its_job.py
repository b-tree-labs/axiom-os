# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``kill -9`` of the orchestrator mid-fire must not wedge the job (ADR-182).

A real orchestrator process, a real TimescaleDB in an ``orch-<hex>`` container
(never the developer's database; removed by name, nothing pruned), and the real
``data.adopt_tick`` skill as the fire. The fire is made to stall by holding a
lock on the source table, so the process is killed while its fire is
``pending``. A plain restart (no supervisor, no SIGUSR1: this is what systemd
does) must then:

- mark that fire ``interrupted`` as soon as it starts, however young the fire;
- leave a fire whose owner is alive alone;
- not be blocked by the dead process's single-flight advisory lock, and run the
  next fire within one cadence interval.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from axiom.extensions.builtins.data_platform.tests import timescale_container
from axiom.extensions.builtins.data_platform.timeseries import adopt_service as svc

SCHEMA = "orch_kill"
TABLE = "readings"
# Seconds between fires: "one cadence interval". The scheduler's idempotency
# bucket is 60 s, so a shorter cadence would share buckets and prove nothing.
EVERY = 60


@pytest.fixture(scope="module")
def dsn():
    os.environ[timescale_container.PREFIX_ENV] = "orch"
    try:
        # A dead client is noticed by its backend within a second, so the
        # advisory lock a killed orchestrator held is released promptly even
        # while the backend is blocked inside a statement.
        name, url = timescale_container.start(
            postgres_args=["postgres", "-c", "client_connection_check_interval=500"])
    finally:
        os.environ.pop(timescale_container.PREFIX_ENV, None)
    assert name.startswith("orch-")
    yield url
    timescale_container.stop(name)


@pytest.fixture
def world(dsn, tmp_path):
    """The source table, the schedule schema, the job, and an env for the process."""
    env = {k: v for k, v in os.environ.items()
           if k not in (svc.DSN_ENV, svc.DIRECT_DSN_ENV, "AXIOM_PROC_MARK", "AXIOM_TEST_SCHEMA_SUFFIX")}
    state = tmp_path / "state"
    state.mkdir()
    env.update({"AXIOM_DB_URL": dsn, "AXI_STATE_DIR": str(state), "AXI_ORCHESTRATOR_TICK_SECONDS": "1",
                "AXI_CHANGE_LEDGER": str(tmp_path / "changes.jsonl"), "PYTHONUNBUFFERED": "1"})
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")
        c.execute("DROP SCHEMA IF EXISTS schedule CASCADE")
        c.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        c.execute("DROP TABLE IF EXISTS data_platform.timeseries_adoption_windows")
        c.execute("DROP TABLE IF EXISTS data_platform.timeseries_adoptions")
        c.execute(f"CREATE SCHEMA {SCHEMA}")
        c.execute(f"CREATE TABLE {SCHEMA}.{TABLE} (site text NOT NULL, channel text NOT NULL, "
                  "ts timestamptz NOT NULL, value double precision, UNIQUE (site, channel, ts))")
        c.execute(f"CREATE INDEX ON {SCHEMA}.{TABLE} (ts)")
        end = datetime.now(UTC) - timedelta(hours=3)
        c.execute(f"INSERT INTO {SCHEMA}.{TABLE} SELECT 'a', 'c1', %s - make_interval(mins => i), 20 + (i %% 50) * 0.01 "
                  "FROM generate_series(0, 2999) i", (end,))
        c.execute(f"ANALYZE {SCHEMA}.{TABLE}")
    done = subprocess.run(
        [sys.executable, "-c", "from axiom.infra.db import provision_extension; "
         "r = provision_extension('schedule'); print(r)"], env=env, capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    svc.schedule(state, f"{SCHEMA}.{TABLE}",
                 params={"identity": "site,channel", "window": "12h", "dsn": dsn, "lock_timeout": "300s"},
                 every_seconds=EVERY, batch_seconds=60, ready_timeout_seconds=20,
                 change_ledger=tmp_path / "changes.jsonl")
    # Register the cadence now and make its first fire due in two seconds, so the
    # test does not sit through a whole interval before the fire it will kill.
    done = subprocess.run(
        [sys.executable, "-c", "from axiom.extensions.builtins.data_platform.timeseries import adopt_service as s; "
         f"print(s.sync_cadences({str(state)!r}))"], env=env, capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute("UPDATE schedule.schedule_definition SET next_fire_at = now() + interval '2 seconds' "
                  "WHERE action = 'data.adopt_tick'")
    yield env, state
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute("DROP SCHEMA IF EXISTS schedule CASCADE")


def _fires(dsn):
    with psycopg.connect(dsn, autocommit=True) as c:
        return c.execute(
            "SELECT outcome, owner_pid, intended_fire_at FROM schedule.schedule_fire_log f "
            "JOIN schedule.schedule_definition d ON d.id = f.schedule_id WHERE d.action = 'data.adopt_tick' "
            "ORDER BY intended_fire_at").fetchall()


def _wait(cond, what, timeout=90.0, show=None):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        got = cond()
        if got:
            return got
        time.sleep(0.25)
    raise AssertionError(f"timed out waiting for {what}" + (f"; saw {show()}" if show else ""))


def _start(env, tmp_path, tag):
    log = open(tmp_path / f"orchestrator-{tag}.log", "wb")
    return subprocess.Popen(
        [sys.executable, "-m", "axiom.extensions.builtins.data_platform.orchestration"],
        env=env, stdout=log, stderr=subprocess.STDOUT,
        # Its own process group, so the test can reap the fire the orchestrator
        # forked: killing only the orchestrator leaves that child running with
        # AXIOM_DB_URL in its environment, which the credential-sweep tests then
        # find on the same CI runner (main CI at 9f4429f0).
        start_new_session=True)


def _reap(p):
    """Kill the orchestrator's whole process group, even if the leader is already dead."""
    try:
        os.killpg(p.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    if p.poll() is None:
        p.wait()


def test_a_kill_9_mid_fire_does_not_wedge_the_next_fire(dsn, world, tmp_path):
    env, state = world
    store = svc.JobStore(state)
    holder = psycopg.connect(dsn, autocommit=False)
    holder.execute(f"LOCK TABLE {SCHEMA}.{TABLE} IN ACCESS EXCLUSIVE MODE")  # the fire will stall here
    first = _start(env, tmp_path, "first")
    second = None
    try:
        pending = _wait(lambda: [f for f in _fires(dsn) if f[0] == "pending"], "a fire to be in flight")
        assert pending[0][1] == first.pid, "the claim names the process that took it"

        os.kill(first.pid, signal.SIGKILL)
        first.wait(timeout=30)
        assert [f[0] for f in _fires(dsn)] == ["pending"], "the dead process left its fire pending"
        killed_at = time.monotonic()
        holder.rollback()  # whatever the dead process was waiting on is free now

        # A plain restart: no supervisor, no signal.
        second = _start(env, tmp_path, "second")
        _wait(lambda: "interrupted" in [f[0] for f in _fires(dsn)], "the orphaned fire to be reconciled", 30)
        assert time.monotonic() - killed_at < 300, "recovered at once, not after the 300 s age"

        # The next fire runs and finishes the job: the dead process's single-flight
        # lock went with its connection, and nothing waited for it.
        _wait(lambda: store.get(f"{SCHEMA}.{TABLE}").state == "done",
              "the next fire to run and finish the copy", EVERY + 40,
              show=lambda: (_fires(dsn), store.get(f"{SCHEMA}.{TABLE}")))
        # The job file says done a moment before the fire's outcome is recorded.
        _wait(lambda: "success" in [f[0] for f in _fires(dsn)][1:], "the fire's outcome to be recorded", 20)
        outcomes = [f[0] for f in _fires(dsn)]
        assert outcomes[0] == "interrupted" and "success" in outcomes[1:], outcomes
        # ... within one cadence interval of the instant that was interrupted.
        intended = [f[2] for f in _fires(dsn)]
        assert (intended[1] - intended[0]).total_seconds() <= EVERY + 5
    finally:
        holder.close()
        for p in (first, second):
            if p is not None:
                _reap(p)


def test_a_fire_owned_by_a_live_orchestrator_is_left_alone_by_a_second_copy(dsn, world, tmp_path):
    env, state = world
    holder = psycopg.connect(dsn, autocommit=False)
    holder.execute(f"LOCK TABLE {SCHEMA}.{TABLE} IN ACCESS EXCLUSIVE MODE")
    old = _start(env, tmp_path, "old")
    new = None
    try:
        _wait(lambda: [f for f in _fires(dsn) if f[0] == "pending"], "a fire to be in flight")
        new = _start(env, tmp_path, "new")  # an overlapping copy, as in a supervised switch
        time.sleep(15)  # many ticks of the second copy, including its start-up reconcile
        assert [f[0] for f in _fires(dsn)] == ["pending"], "the live copy's fire was not touched"
        assert old.poll() is None and new.poll() is None
    finally:
        holder.rollback()
        holder.close()
        for p in (old, new):
            if p is not None:
                _reap(p)
