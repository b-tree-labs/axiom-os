# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Switching a site between contributor and local-first (ADR-180 §3, §5).

The mode is the node's ``record_of_truth``; reconcile reads it from the node
when not told. Across contributor → local-first → contributor, on the same two
copies:

- what the site's [share] policy excludes never reaches the host, in EITHER
  mode (found while writing this: contributor mode ignored the policy, so a
  feed the site had withheld leaked to the host through reconcile);
- the host fills a local gap only in contributor mode, or when the site opts in;
- a conflict is flagged in every mode and resolved in none;
- no switch deletes anything on either side.
"""

from __future__ import annotations

import hashlib

import psycopg
import pytest

from axiom.extensions.builtins.data_platform.conformance import SILVER_SIGNALS_DDL, pg_upsert
from axiom.extensions.builtins.data_platform.conformance.reconcile import PgSide, reconcile
from axiom.infra import node_functions as nf

SITE = "site-a"


@pytest.fixture
def node(tmp_path, monkeypatch):
    path = tmp_path / "node.toml"
    monkeypatch.setenv(nf.CONFIG_ENV, str(path))
    return path


@pytest.fixture
def sides(timescale_dsn):
    made = []
    for name in ("mode_local", "mode_upstream"):
        with psycopg.connect(timescale_dsn, autocommit=True) as admin:
            admin.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
            admin.execute(f"CREATE DATABASE {name}")
        c = psycopg.connect(timescale_dsn.rsplit("/", 1)[0] + "/" + name, autocommit=True)
        for stmt in SILVER_SIGNALS_DDL:
            c.execute(stmt)
        made.append(c)
    yield made[0], made[1]
    for c in made:
        c.close()


def _row(minute, feed="site-a.live", value=None):
    ts = f"2026-10-08T10:{minute:02d}:00+00:00"
    return {"site": SITE, "feed": feed, "channel": "TC1", "ts": ts,
            "value": float(minute) if value is None else value, "unit": "degC", "quality": "good",
            "source_class": "measured", "schema_ref": "site-a/v1",
            "row_hash": hashlib.sha256(f"{feed}|{ts}".encode()).hexdigest()}


def _load(conn, rows):
    up = pg_upsert(conn.cursor())
    for r in rows:
        up(r)


def _have(conn, feed, minute) -> bool:
    return conn.execute(
        "SELECT count(*) FROM silver.signals WHERE feed = %s AND ts = %s",
        (feed, f"2026-10-08T10:{minute:02d}:00+00:00")).fetchone()[0] == 1


def _total(conn) -> int:
    return conn.execute("SELECT count(*) FROM silver.signals").fetchone()[0]


def _shares_live_only(row) -> bool:
    return row["feed"] == "site-a.live"


def _reconcile(local, upstream):
    """As a scheduled job runs it: the mode comes from the node, not the caller."""
    return reconcile(PgSide(local), PgSide(upstream), site=SITE,
                     start="2026-10-08T00:00:00+00:00", end="2026-10-09T00:00:00+00:00",
                     may_share=_shares_live_only)


def test_contributor_to_local_first_and_back(sides, node):
    local, upstream = sides
    _load(local, [_row(1), _row(2, feed="site-a.aux"), _row(9, value=1.0)])
    _load(upstream, [_row(3), _row(9, value=2.0)])
    totals = []

    # contributor (undeclared)
    r = _reconcile(local, upstream)
    assert r.record_of_truth == "upstream"
    assert _have(upstream, "site-a.live", 1)
    assert not _have(upstream, "site-a.aux", 2)          # the policy holds in contributor mode
    assert r.not_shared == 1
    assert _have(local, "site-a.live", 3)                 # the host fills the local gap
    assert r.conflicts == 1
    totals.append((_total(local), _total(upstream)))

    # local-first
    nf.set_record_of_truth("local", path=node)
    _load(upstream, [_row(4)])                            # appears at the host only
    _load(local, [_row(5)])
    r = _reconcile(local, upstream)
    assert r.record_of_truth == "local"
    assert _have(upstream, "site-a.live", 5)              # shared from local
    assert not _have(local, "site-a.live", 4)             # not pulled down: the site did not opt in
    assert r.not_filled_by_choice == 1
    assert not _have(upstream, "site-a.aux", 2)
    assert r.conflicts == 1                               # still flagged, still unresolved
    totals.append((_total(local), _total(upstream)))

    # back to contributor
    nf.set_record_of_truth("upstream", path=node)
    r = _reconcile(local, upstream)
    assert _have(local, "site-a.live", 4)                 # now the host fills it
    assert not _have(upstream, "site-a.aux", 2)
    assert r.conflicts == 1
    totals.append((_total(local), _total(upstream)))

    # both values of the conflict are still there, on their own sides
    assert local.execute("SELECT value FROM silver.signals WHERE ts = %s",
                         ("2026-10-08T10:09:00+00:00",)).fetchone()[0] == 1.0
    assert upstream.execute("SELECT value FROM silver.signals WHERE ts = %s",
                            ("2026-10-08T10:09:00+00:00",)).fetchone()[0] == 2.0
    # no switch deleted anything, on either side
    for (l0, u0), (l1, u1) in zip(totals, totals[1:]):
        assert l1 >= l0 and u1 >= u0


def test_an_explicit_caller_choice_still_wins_over_the_node(sides, node):
    local, upstream = sides
    nf.set_record_of_truth("local", path=node)
    _load(upstream, [_row(3)])
    r = reconcile(PgSide(local), PgSide(upstream), site=SITE, record_of_truth="upstream",
                  start="2026-10-08T00:00:00+00:00", end="2026-10-09T00:00:00+00:00")
    assert r.record_of_truth == "upstream" and _have(local, "site-a.live", 3)
