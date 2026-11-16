# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The swap's exclusive lock is short because the tail is copied outside it.

The copy's boundary is fixed when it begins, so by the swap the source holds
everything written since: on a long copy, hours. The first implementation
copied all of that under ACCESS EXCLUSIVE (62 s on the node that motivated
this). Now the tail is caught up in passes with the writer running, each pass
copying what the last did not, until a pass is under a threshold; only a final
pass over the last moments runs under the lock.

Against a real TimescaleDB (``af-*`` container; never the developer's database).
"""

from __future__ import annotations

import threading
import time
from datetime import timedelta

import psycopg
import pytest

from axiom.extensions.builtins.data_platform.timeseries import adopt
from axiom.extensions.builtins.data_platform.timeseries.contract import TimeseriesContract

from .conftest import SCHEMA

TAIL_ROWS = 400_000


def _c() -> TimeseriesContract:
    return TimeseriesContract(schema=SCHEMA, table="readings", identity=("site", "feed", "channel"),
                              chunk_interval=timedelta(hours=12))


@pytest.fixture
def copied(db):
    db.execute("DROP TABLE IF EXISTS data_platform.timeseries_adoption_windows")
    db.execute("DROP TABLE IF EXISTS data_platform.timeseries_adoptions")
    db.execute(f"CREATE TABLE {SCHEMA}.readings (site text NOT NULL, feed text NOT NULL, channel text NOT NULL, "
               "ts timestamptz NOT NULL, value double precision, UNIQUE (site, feed, channel, ts))")
    db.execute(f"CREATE INDEX readings_ts ON {SCHEMA}.readings (ts)")
    db.execute(f"INSERT INTO {SCHEMA}.readings SELECT 'a', 'f', 'c', now() - interval '3 days' - make_interval(secs => i), 1 "
               "FROM generate_series(0, 100000) i")
    db.execute(f"ANALYZE {SCHEMA}.readings")
    adopt.run(db, _c())
    # Everything written since the copy's boundary: the tail a long copy leaves behind.
    boundary = adopt.progress(db, _c()).last_window_end
    db.execute(f"INSERT INTO {SCHEMA}.readings SELECT 'a', 'f', 'tail', %s + make_interval(secs => i * 0.01), 2 "
               "FROM generate_series(0, %s) i", (boundary, TAIL_ROWS - 1))
    return TAIL_ROWS


class _Latency(threading.Thread):
    """A writer that records its slowest insert: what the lock costs a caller."""

    def __init__(self, dsn: str) -> None:
        super().__init__(daemon=True)
        self.dsn, self.worst, self.rows, self._halt = dsn, 0.0, 0, threading.Event()

    def run(self) -> None:
        with psycopg.connect(self.dsn, autocommit=True) as c:
            while not self._halt.is_set():
                t0 = time.monotonic()
                c.execute(f"INSERT INTO {SCHEMA}.readings VALUES ('a', 'f', 'live', now(), 1.0) ON CONFLICT DO NOTHING")
                self.worst = max(self.worst, time.monotonic() - t0)
                self.rows += 1
                time.sleep(0.002)

    def stop(self) -> None:
        self._halt.set()
        self.join(timeout=10)


def test_a_large_tail_is_caught_up_outside_the_lock_and_the_lock_stays_short(timescale_dsn, db, copied):
    w = _Latency(timescale_dsn)
    w.start()
    time.sleep(0.2)
    try:
        steps = adopt.swap(db, _c())
    finally:
        w.stop()
    info = adopt.last_swap(db, _c())
    assert info["tail_rows_in_lock"] < 20_000, info
    assert info["tail_rows_outside_lock"] >= TAIL_ROWS, "the bulk of the tail went in outside the lock"
    assert info["lock_held_seconds"] < 1.0, info
    assert w.worst < 3.0, f"the writer's slowest insert was {w.worst:.2f}s"
    assert any("under a lock held" in s and "outside" in s for s in steps), steps
    n = db.execute(f"SELECT count(*) FROM {SCHEMA}.readings").fetchone()[0]
    assert n == 100_001 + TAIL_ROWS + w.rows, "every row, none twice"
    assert adopt.verify(db, _c()).ok


def test_a_writer_that_outpaces_the_catch_up_is_refused_not_locked_for(timescale_dsn, db, copied):
    with pytest.raises(adopt.AdoptionRefused) as e:
        adopt.swap(db, _c(), tail_threshold_rows=0, max_catchup_passes=2)
    assert "catch" in str(e.value)
    assert adopt.progress(db, _c()).phase != "swapped"
    assert db.execute(f"SELECT to_regclass('{SCHEMA}.readings__old')").fetchone()[0] is None


def test_a_swap_retried_after_a_refusal_does_not_duplicate_the_tail(db, copied):
    with pytest.raises(adopt.AdoptionRefused):
        adopt.swap(db, _c(), tail_threshold_rows=0, max_catchup_passes=1)
    adopt.swap(db, _c())
    assert db.execute(f"SELECT count(*) FROM {SCHEMA}.readings").fetchone()[0] == 100_001 + TAIL_ROWS
    assert adopt.verify(db, _c()).ok
