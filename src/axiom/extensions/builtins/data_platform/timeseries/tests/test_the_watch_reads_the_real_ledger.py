# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The watch reads the real adoption ledger, not only a fake one.

Against a real TimescaleDB in an ``adopt-<hex>`` container: progress, the size
per copied row, the free space and a window that failed to verify come out of
the ledger the copy writes, and the watch turns the last into a mismatch.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from axiom.extensions.builtins.data_platform.tests import timescale_container
from axiom.extensions.builtins.data_platform.timeseries import adopt_service as svc
from axiom.extensions.builtins.data_platform.timeseries import adopt_watch as w
from axiom.extensions.builtins.data_platform.timeseries.contract import TimeseriesContract

SCHEMA, TABLE = "adopt_watch", "readings"


@pytest.fixture(scope="module")
def dsn():
    os.environ[timescale_container.PREFIX_ENV] = "adopt"
    try:
        name, url = timescale_container.start()
    finally:
        os.environ.pop(timescale_container.PREFIX_ENV, None)
    yield url
    timescale_container.stop(name)


def _contract():
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
        c.execute(f"INSERT INTO {SCHEMA}.{TABLE} SELECT 'a', ch, %s - make_interval(mins => i), 1.5 "
                  "FROM generate_series(0, 4319) i, unnest(%s::text[]) ch", (end, ["c1", "c2"]))
        c.execute(f"ANALYZE {SCHEMA}.{TABLE}")
        yield c


class _Service:
    def ledger(self, job):  # pragma: no cover - replaced below
        raise AssertionError

    def service(self, job):
        return w.ServiceView("active", datetime.now(UTC))


def test_the_real_ledger_gives_progress_size_space_and_a_failed_window(db, dsn, tmp_path, monkeypatch):
    monkeypatch.setenv("AXI_CHANGE_LEDGER", str(tmp_path / "c.jsonl"))
    monkeypatch.setenv("AXIOM_DB_DATA_PATH", str(tmp_path))
    state = tmp_path / "state"
    job = svc.schedule(state, f"{SCHEMA}.{TABLE}", params={"identity": "site,channel", "window": "12h", "dsn": dsn},
                       every_seconds=60, batch_seconds=1, change_ledger=tmp_path / "c.jsonl")
    st = svc.JobStore(state)
    ticks = iter(range(0, 10_000, 2))
    svc.run_tick(job, _contract(), st, clock=lambda: float(next(ticks)))

    src = w.PgSource(contract_for=lambda j: _contract())
    view = src.ledger(st.get(job.table))
    assert view.phase == "copying" and view.windows_done >= 1 and view.windows_done < view.windows_total
    assert view.rows_copied > 0 and view.bytes_per_row and view.bytes_per_row > 0
    assert view.free_bytes and view.free_bytes > 0 and view.last_failure is None
    assert view.updated_at is not None

    class S(_Service):
        def ledger(self, j):
            return src.ledger(j)

    assert w.evaluate([st.get(job.table)], S(), datetime.now(UTC)) == [], "a copy that is advancing is quiet"

    # A window that will not verify is kept in the ledger and surfaces as a mismatch.
    def corrupt(conn, c, lo, hi, n):
        conn.execute(
            f'DELETE FROM {SCHEMA}."{TABLE}__adopt" WHERE ts = (SELECT min(ts) FROM {SCHEMA}."{TABLE}__adopt" '
            "WHERE ts >= %s AND ts < %s)", (lo, hi))

    out = svc.run_tick(st.get(job.table), _contract(), st, after_copy=corrupt)
    assert out.status == "halted"
    failed = src.ledger(st.get(job.table))
    assert failed.last_failure and failed.last_failure["message"].startswith("row count differs")
    faults = w.evaluate([st.get(job.table)], S(), datetime.now(UTC))
    assert [f.kind for f in faults] == ["mismatch"] and faults[0].cause == "ours" and faults[0].latched
