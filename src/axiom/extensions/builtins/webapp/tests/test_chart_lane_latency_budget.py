# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A chart over a large channel answers within budget (#1827).

A deployed node's chart lane took ~30 s per figure on a channel of tens of millions of
readings, because it scanned the whole record on every request. This loads a synthetic
channel of ``AXIOM_CHART_BUDGET_ROWS`` readings (10 M by default) into a throwaway schema the
test owns, indexed the way gold's readings are, and holds the drawing query to a budget: a
one-day figure, and a figure with no window at all (which must not become a scan), each under
two seconds. A regression fails CI rather than a node.

Runs against a real Postgres (``AXIOM_PG_TEST_URL``, or a ``postgresql`` ``AXIOM_DB_URL`` such as
the pre-push hook's throwaway database or CI's integration service); skips without one. Marked
``integration`` so the fast unit set never pays for the load.
"""

from __future__ import annotations

import os
import time
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from axiom.extensions.builtins.webapp.catalog import series as series_mod

pytestmark = pytest.mark.integration

ROWS = int(os.environ.get("AXIOM_CHART_BUDGET_ROWS", "10000000"))
BUDGET_S = 2.0
CADENCE_S = 1
T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _pg_url() -> str | None:
    for name in ("AXIOM_PG_TEST_URL", "AXIOM_DB_URL"):
        url = os.environ.get(name, "")
        if url.startswith(("postgresql", "postgres:")):
            return url
    return None


@pytest.fixture(scope="module")
def big_channel():
    url = _pg_url()
    if not url:
        pytest.skip("needs a real Postgres (AXIOM_PG_TEST_URL or a postgresql AXIOM_DB_URL)")
    from sqlalchemy import create_engine, text
    from sqlalchemy.exc import OperationalError
    from sqlalchemy.orm import Session

    engine = create_engine(url)
    schema = f"chart_budget_{uuid.uuid4().hex[:10]}"
    try:
        with engine.begin() as c:
            c.execute(text(f"CREATE SCHEMA {schema}"))
    except OperationalError as exc:
        pytest.skip(f"Postgres not reachable: {exc.orig}")
    try:
        t_load = time.perf_counter()
        with engine.begin() as c:
            c.execute(text(
                f"CREATE TABLE {schema}.signals (site text, feed text, channel text, "
                "ts timestamptz, value double precision, unit text)"))
            c.execute(text(
                f"INSERT INTO {schema}.signals "
                "SELECT 'site-a', 'alpha', 'big', "
                "  CAST(:t0 AS timestamptz) + g * CAST(:cadence AS interval), "
                "  sin(g / 600.0) * 50 + 50, 'degC' "
                "FROM generate_series(0, :n - 1) AS g"),
                {"t0": T0.isoformat(), "cadence": f"{CADENCE_S} seconds", "n": ROWS})
            # The index gold's readings carry: an exact prefix of the drawing query's predicate.
            c.execute(text(f"CREATE INDEX ON {schema}.signals (site, feed, channel, ts)"))
            c.execute(text(f"ANALYZE {schema}.signals"))
        load_s = time.perf_counter() - t_load
        with Session(engine) as s:
            yield s, f"{schema}.signals", load_s
    finally:
        with engine.begin() as c:
            c.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
        engine.dispose()


def _timed(call, runs: int = 3) -> float:
    call()  # warm the plan and the cache
    best = None
    for _ in range(runs):
        t0 = time.perf_counter()
        call()
        took = time.perf_counter() - t0
        best = took if best is None else min(best, took)
    return best


def test_a_one_day_figure_over_ten_million_readings_is_within_budget(big_channel):
    s, source, load_s = big_channel
    end = T0 + timedelta(seconds=ROWS * CADENCE_S)
    out = {}

    def call():
        out["v"] = series_mod.bucketed_series(
            s, site="site-a", feed="alpha", channels=["big"], bucket_s=60, source=source,
            t_from=(end - timedelta(days=1)).isoformat(), t_to=end.isoformat())

    took = _timed(call)
    print(f"\n[chart budget] {ROWS:,} readings loaded in {load_s:.1f}s; one-day figure {took * 1000:.0f} ms")
    assert out["v"]["series"], "the one-day figure drew nothing"
    assert took < BUDGET_S, f"a one-day figure took {took:.2f}s over {ROWS:,} readings"


def test_a_figure_with_no_window_is_not_a_scan(big_channel):
    s, source, _ = big_channel
    out = {}

    def call():
        out["v"] = series_mod.bucketed_series(
            s, site="site-a", feed="alpha", channels=["big"], bucket_s=60, source=source)

    took = _timed(call)
    print(f"\n[chart budget] unwindowed figure {took * 1000:.0f} ms")
    assert len(out["v"]["series"]) == series_mod.MAX_SERIES_POINTS
    assert took < BUDGET_S, f"an unwindowed figure took {took:.2f}s over {ROWS:,} readings"
