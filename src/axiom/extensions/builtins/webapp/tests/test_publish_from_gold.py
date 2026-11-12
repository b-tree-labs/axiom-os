# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""W13: the app database is published from gold, says how fresh it is, and rebuilds the same.

SQLite only (ADR-174: tests never reach a real database). A second SQLite file
attached as ``gold`` plays the gold schema, so the publication reads
``gold.signals`` by its real name. Test names carry the acceptance row they prove.
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session

from axiom.extensions.builtins.webapp.catalog import models, publish, store

BASE = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)


@pytest.fixture
def session(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'app.db'}")
    gold_file = tmp_path / "gold.db"

    @event.listens_for(engine, "connect")
    def _attach(dbapi_conn, _record):
        dbapi_conn.execute(f"ATTACH DATABASE '{gold_file}' AS gold")

    import axiom.extensions.builtins.webapp.views.models  # noqa: F401 - application state tables

    models.Base.metadata.create_all(engine)
    s = Session(engine)
    s.execute(text("CREATE TABLE gold.signals (site TEXT, feed TEXT, channel TEXT, ts TEXT, value REAL, unit TEXT)"))
    s.commit()

    @contextlib.contextmanager
    def _provider():
        yield s

    monkeypatch.setattr(store, "_provider", _provider)
    publish.reset()
    yield s
    s.close()
    publish.reset()


def _add(s, channel, minutes, site="site-a", feed="alpha"):
    for m in minutes:
        s.execute(text("INSERT INTO gold.signals VALUES (:a, :b, :c, :d, 1.0, 'degC')"),
                  {"a": site, "b": feed, "c": channel, "d": (BASE + timedelta(minutes=m)).isoformat()})
    s.commit()


def _catalog(s):
    return [tuple(r) for r in s.execute(text(
        "SELECT site, feed, channel, unit, rows, first_ts, last_ts FROM site_catalog_channel "
        "ORDER BY site, feed, channel"))]


# ------------------------------------------------------------------ A4: gold source and version


def test_mvp_11_a4_every_published_table_names_its_gold_source_and_version(session):
    _add(session, "ch1", range(3))
    out = publish.publish(session, "site_catalog_channel", now=BASE)
    row = session.get(models.PublicationState, "site_catalog_channel")
    assert row.gold_object == "gold.signals" and out["gold_object"] == "gold.signals"
    assert row.gold_version and row.version_kind == "watermark" and row.rows == 1
    assert row.freshness_s == 15 * 60 and row.invalidate_on == "both"


def test_mvp_11_a4_nothing_in_the_app_database_is_copied_from_silver(session):
    # The registry: every publication's source is a gold object...
    for pub in publish.publications().values():
        assert publish.require_gold(pub.gold_object).startswith("gold.")
    # ...and one that is not is refused at registration, whatever it is called.
    for bad in ("silver.signals", "bronze.raw", "public.signals", "signals", "gold_signals"):
        with pytest.raises(publish.SilverNeverServes, match="silver never serves"):
            publish.register(publish.Publication(
                gold_object=bad, app_table="x", freshness=timedelta(minutes=1), invalidate_on="schedule",
                project=lambda s, f: None, clear=lambda s: None))
    # ...and the projection skill refuses a silver source before running anything.
    from axiom.extensions.builtins.webapp.skills.project import run

    refused = run({"source": "silver.signals"})
    assert not refused.ok and "silver never serves" in refused.errors[0]


def test_mvp_11_a4_every_table_in_the_schema_is_a_publication_or_application_state(session):
    tables = set(models.Base.metadata.tables)
    published = set(publish.publications())
    assert tables == published | publish.APP_STATE_TABLES | {publish.LEDGER_TABLE}, (
        "a table in the app database is neither published from gold nor application state"
    )
    assert not (published & publish.APP_STATE_TABLES)


def test_a_gold_revision_is_preferred_over_the_watermark(session):
    session.execute(text("CREATE TABLE gold.signal_catalogue (site TEXT, feed TEXT, channel TEXT, computed_at TEXT)"))
    session.execute(text("INSERT INTO gold.signal_catalogue VALUES ('site-a', 'alpha', 'ch1', '2026-10-30T12:05:00+00:00')"))
    session.commit()
    _add(session, "ch1", range(2))
    out = publish.publish(session, "site_catalog_channel", now=BASE)
    assert out["version_kind"] == "revision" and out["gold_version"] == "2026-10-30T12:05:00+00:00"


# ------------------------------------------------------------------ A5: freshness and invalidation


def test_mvp_11_a5_a_gold_change_reaches_the_app_database_on_the_next_refresh(session):
    session.execute(text("CREATE TABLE gold.signal_catalogue (site TEXT, feed TEXT, channel TEXT, computed_at TEXT)"))
    session.execute(text("INSERT INTO gold.signal_catalogue VALUES ('site-a', 'alpha', 'ch1', '2026-10-30T12:00:00+00:00')"))
    session.commit()
    _add(session, "ch1", range(2))
    publish.refresh(session, "site_catalog_channel", now=BASE)
    # Gold unchanged: nothing to do, and the table is current however long ago it ran.
    assert publish.refresh(session, "site_catalog_channel")["unchanged"] is True
    fresh = publish.freshness(session, "site_catalog_channel", now=BASE + timedelta(hours=3))
    assert fresh["stale"] is False and fresh["lag_s"] == 0
    # Gold moves (conform wrote rows and stamped its catalogue): the next refresh publishes them.
    _add(session, "ch1", [5, 6])
    session.execute(text("UPDATE gold.signal_catalogue SET computed_at = '2026-10-30T12:06:00+00:00'"))
    session.commit()
    assert publish.freshness(session, "site_catalog_channel", now=BASE + timedelta(hours=3))["stale"] is True
    out = publish.refresh(session, "site_catalog_channel", now=BASE + timedelta(hours=3))
    assert not out.get("unchanged") and out["gold_version"] == "2026-10-30T12:06:00+00:00"
    assert _catalog(session)[0][4] == 4


def test_mvp_11_a5_past_its_declared_freshness_the_page_shows_as_of_with_the_lag(session):
    _add(session, "ch1", range(2))
    publish.publish(session, "site_catalog_channel", now=BASE)
    inside = publish.freshness(session, "site_catalog_channel", now=BASE + timedelta(minutes=10))
    assert inside["stale"] is False and inside["note"] == ""
    late = publish.freshness(session, "site_catalog_channel", now=BASE + timedelta(minutes=40))
    assert late["stale"] is True and late["lag_s"] == 40 * 60
    assert late["note"] == "as of 2026-10-30 12:00 UTC, 40 min behind"


def test_mvp_11_a5_the_catalog_api_carries_as_of(session, monkeypatch):
    from fastapi import APIRouter, FastAPI
    from fastapi.testclient import TestClient

    from axiom.extensions.builtins.webapp.api import catalog as catalog_api

    monkeypatch.setattr(catalog_api, "_bound", lambda: False)
    monkeypatch.setattr(catalog_api, "_require_site", lambda site: None)
    _add(session, "ch1", range(2))
    publish.publish(session, "site_catalog_channel", now=datetime.now(UTC) - timedelta(hours=2))
    router = APIRouter()
    catalog_api.register_catalog_routes(router)
    app = FastAPI()
    app.include_router(router)
    body = TestClient(app).get("/sites/site-a/channels").json()
    assert body["as_of"]["stale"] is True and body["as_of"]["note"].startswith("as of ")
    assert body["as_of"]["gold_object"] == "gold.signals"


def test_a_table_never_published_is_stale_and_says_so(session):
    fresh = publish.freshness(session, "site_catalog_channel")
    assert fresh["stale"] is True and fresh["note"] == "not yet published from gold"


# ------------------------------------------------------------------ A6: rebuild


def test_mvp_11_a6_rebuilding_from_gold_gives_the_same_rows_and_leaves_application_state_alone(session):
    from axiom.extensions.builtins.webapp.views.models import ChannelLabel, SavedView

    _add(session, "ch1", range(3))
    _add(session, "ch2", range(4), site="site-b")
    publish.publish(session, "site_catalog_channel", now=BASE)
    _add(session, "ch1", [10, 11])  # an incremental pass widens what it knows
    publish.publish(session, "site_catalog_channel", now=BASE + timedelta(minutes=15))
    before = _catalog(session)
    assert len(before) == 2 and before[0][4] == 5

    session.add(ChannelLabel(site="site-a", feed="alpha", channel="ch1", label="Inlet temperature",
                             set_by="@alice:site-a"))
    session.add(SavedView(**_saved_view_fields(SavedView)))
    session.commit()
    state_before = (_rows(session, "channel_label"), _rows(session, "saved_view"))
    assert all(state_before)

    publish.rebuild(session, now=BASE + timedelta(minutes=30))
    assert _catalog(session) == before
    publish.rebuild(session, now=BASE + timedelta(minutes=45))  # and again: idempotent
    assert _catalog(session) == before
    assert (_rows(session, "channel_label"), _rows(session, "saved_view")) == state_before


def _rows(session, table):
    return [tuple(r) for r in session.execute(text(f"SELECT * FROM {table} ORDER BY 1"))]


def _saved_view_fields(model):
    """A saved view with every required column filled, whatever the model's current shape."""
    out = {}
    for col in model.__table__.columns:
        if col.default is not None or col.nullable or col.server_default is not None:
            continue
        python_type = getattr(col.type, "python_type", str)
        out[col.name] = BASE if python_type is datetime else ("v1" if python_type is str else 0)
    return out


# ------------------------------------------------------------------ the relation check builds no SQL from a name


def test_relation_exists_asks_the_inspector_and_builds_no_sql_from_the_relation_name(session):
    """A table and a view in gold are found; a missing name, an unattached
    schema and a hostile name answer False without raising or running anything."""
    session.execute(text("CREATE VIEW gold.latest AS SELECT channel, max(ts) AS ts FROM gold.signals GROUP BY channel"))
    assert publish._relation_exists(session, "gold.signals") is True
    assert publish._relation_exists(session, "gold.latest") is True
    assert publish._relation_exists(session, "gold.nowhere") is False
    assert publish._relation_exists(session, "silver.signals") is False
    assert publish._relation_exists(session, 'gold"; DROP TABLE gold.signals; --.signals') is False
    assert publish._relation_exists(session, "gold.signals") is True
