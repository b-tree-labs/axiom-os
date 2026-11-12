# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The chart lane never scans a channel's whole record unasked (#1827).

A deployed node's interim chart lane computed the value range over every reading a channel
ever had on every figure request: ~30 s on tens of millions of readings, inside an
``async def`` that held the event loop. The platform's own lane keeps two rules:

1. a read over the readings table always carries a time window. With none asked, the window
   is the most points one answer can hold, ending at the channel's newest reading, which the
   serving catalogue already knows (so finding it is not a scan either);
2. no data-lane route is an ``async def`` that makes a blocking call (see
   ``test_no_async_route_blocks``).

SQL is captured at the engine's ``before_cursor_execute`` seam.
"""

from __future__ import annotations

import contextlib
import re
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session

from axiom.extensions.builtins.webapp.catalog import models, store, sync
from axiom.extensions.builtins.webapp.catalog import series as series_mod

BASE = datetime(2026, 9, 15, tzinfo=UTC)


@pytest.fixture
def session(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'w.db'}")
    models.Base.metadata.create_all(engine)
    s = Session(engine)
    s.execute(text("CREATE TABLE signals (site TEXT, feed TEXT, channel TEXT, ts TEXT, "
                   "value REAL, unit TEXT)"))
    # Three days of minutes; the newest day is the one an unwindowed chart opens on.
    for i in range(3 * 1440):
        s.execute(text("INSERT INTO signals VALUES ('site-a','alpha','ch1',:t,:v,'degC')"),
                  {"t": (BASE + timedelta(minutes=i)).isoformat(), "v": float(i)})
    s.commit()

    @contextlib.contextmanager
    def _provider():
        yield s

    monkeypatch.setattr(store, "_provider", _provider)
    yield s
    s.close()


@pytest.fixture
def statements(session):
    seen: list[str] = []

    def _capture(conn, cursor, statement, parameters, context, executemany):  # noqa: ARG001
        seen.append(" ".join(statement.split()))

    event.listen(session.get_bind(), "before_cursor_execute", _capture)
    yield seen
    event.remove(session.get_bind(), "before_cursor_execute", _capture)


def _readings(seen: list[str]) -> list[str]:
    """Statements that read the readings table (not the catalogue)."""
    return [s for s in seen if re.search(r"\bFROM signals\b", s)]


def _time_bounded(statement: str) -> bool:
    has_lo = bool(re.search(r"ts\)? >=", statement))
    has_hi = bool(re.search(r"ts\)? <", statement))
    return has_lo and has_hi


def test_an_unwindowed_series_reads_a_bounded_window_ending_at_the_newest_reading(
        session, statements):
    sync.project_catalog(session, source="signals")
    statements.clear()

    out = series_mod.bucketed_series(session, site="site-a", feed="alpha", channels=["ch1"],
                                     bucket_s=60, source="signals")

    reads = _readings(statements)
    assert reads, "the series read nothing"
    assert all(_time_bounded(s) for s in reads), f"a time-unbounded read: {reads}"
    # It opens on the newest readings, not the oldest the cap happened to reach.
    newest = BASE + timedelta(minutes=3 * 1440 - 1)
    assert out["series"][-1]["ts"] == newest.strftime("%Y-%m-%dT%H:%M:%SZ")
    assert len(out["series"]) == series_mod.MAX_SERIES_POINTS


def test_the_newest_reading_comes_from_the_catalogue_when_it_holds_one(session, statements):
    sync.project_catalog(session, source="signals")
    statements.clear()

    series_mod.bucketed_series(session, site="site-a", feed="alpha", channels=["ch1"],
                               bucket_s=3600, source="signals")

    assert not [s for s in _readings(statements) if "ORDER BY ts DESC" in s], (
        "the catalogue knows the newest reading; the readings table was asked anyway")


def test_without_a_catalogue_row_the_newest_reading_is_one_index_seek(session, statements):
    series_mod.bucketed_series(session, site="site-a", feed="alpha", channels=["ch1"],
                               bucket_s=3600, source="signals")

    reads = _readings(statements)
    seeks = [s for s in reads if "ORDER BY ts DESC" in s]
    assert seeks and all("LIMIT" in s for s in seeks), seeks
    assert all(_time_bounded(s) for s in reads if s not in seeks), reads


@pytest.mark.parametrize("given", ["t_from", "t_to"])
def test_one_open_end_is_closed_at_the_most_one_answer_can_hold(session, statements, given):
    bound = (BASE + timedelta(days=1)).isoformat()
    series_mod.bucketed_series(session, site="site-a", feed="alpha", channels=["ch1"],
                               bucket_s=60, source="signals", **{given: bound})

    reads = _readings(statements)
    assert reads and all(_time_bounded(s) for s in reads), reads


def test_an_explicit_window_is_read_as_asked(session):
    out = series_mod.bucketed_series(
        session, site="site-a", feed="alpha", channels=["ch1"], bucket_s=60, source="signals",
        t_from=(BASE + timedelta(hours=1)).isoformat(),
        t_to=(BASE + timedelta(hours=2)).isoformat())

    assert len(out["series"]) == 60
    assert out["series"][0]["ts"] == "2026-09-15T01:00:00Z"
