# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The serving catalog, as platform rather than consumer code.

This tier was built inside a domain consumer and then found to contain no domain
logic at all: 604 lines whose only consumer-specific content was the package
name and its docstrings. The model is (site, stream, channel) — ADR-050's own
vocabulary — and it already sat on `MountSpec`, `session_for` and the skills
registry. It was platform code wearing a consumer's name.

So these tests assert two things. That the projection and the API behave, and
that the tier stays domain-free: a consumer supplies meaning, the platform
supplies serving.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from axiom.extensions.builtins.webapp.catalog import models, store, sync


@pytest.fixture
def session(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path/'w.db'}")
    models.Base.metadata.create_all(engine)
    s = Session(engine)
    s.execute(text(
        "CREATE TABLE signals (site TEXT, stream TEXT, channel TEXT, ts TEXT, "
        "value REAL, unit TEXT)"))
    s.commit()
    import contextlib

    @contextlib.contextmanager
    def _provider():
        yield s

    monkeypatch.setattr(store, "_provider", _provider)
    yield s
    s.close()


def _add(s, channel, ts, site="site-a", stream="alpha", unit="degC"):
    s.execute(text(
        "INSERT INTO signals (site,stream,channel,ts,value,unit) "
        "VALUES (:a,:b,:c,:d,1.0,:u)"),
        {"a": site, "b": stream, "c": channel, "d": ts.isoformat(), "u": unit})


def _rows(s, channel):
    return s.execute(text(
        "SELECT rows FROM site_catalog_channel WHERE channel=:c"),
        {"c": channel}).scalar()


def test_the_projection_counts_a_new_channel_in_full(session):
    base = datetime(2026, 9, 15, tzinfo=UTC)
    for i in range(5):
        _add(session, "ch1", base + timedelta(minutes=i))
    session.commit()
    sync.project_catalog(session, source="signals")
    assert _rows(session, "ch1") == 5


def test_a_rerun_adds_only_what_arrived(session):
    base = datetime(2026, 9, 15, tzinfo=UTC)
    for i in range(3):
        _add(session, "ch1", base + timedelta(minutes=i))
    session.commit()
    sync.project_catalog(session, source="signals")
    for i in range(3, 7):
        _add(session, "ch1", base + timedelta(minutes=i))
    session.commit()
    sync.project_catalog(session, source="signals")
    assert _rows(session, "ch1") == 7, "the rerun recounted history instead of the delta"


def test_the_api_serves_sites_and_channels(session):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from axiom.extensions.builtins.webapp.api.routers import build_api_router

    base = datetime(2026, 9, 15, tzinfo=UTC)
    _add(session, "ch1", base)
    _add(session, "ch2", base, site="site-b")
    session.commit()
    sync.project_catalog(session, source="signals")

    app = FastAPI()
    app.include_router(build_api_router())
    client = TestClient(app)

    r = client.get("/api/v1/sites")
    assert r.status_code == 200, r.text
    assert {s["site"] for s in r.json()["sites"]} == {"site-a", "site-b"}

    r = client.get("/api/v1/sites/site-a/channels")
    assert r.status_code == 200, r.text
    assert [c["channel"] for c in r.json()["channels"]] == ["ch1"]


def test_the_versioned_surface_still_answers(session):
    """Adding the catalog must not disturb what /api/v1 already served."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from axiom.extensions.builtins.webapp.api.routers import build_api_router

    client = TestClient(FastAPI())
    app = FastAPI()
    app.include_router(build_api_router())
    client = TestClient(app)
    assert client.get("/api/v1/health").json()["status"] == "ok"
    assert client.get("/api/v1/version").json()["api"] == "v1"


def test_the_serving_tier_names_no_domain_consumer():
    """The platform must not know whose site this is.

    Guarded here rather than trusted to review: this code arrived from a
    consumer repo, and the mirror that publishes Axiom refuses a push that
    names one.

    The check delegates to that same mirror guard rather than keeping its own
    copy of the word list. A duplicated list drifts, and — found the hard way —
    a test that spells the forbidden words out in order to forbid them is itself
    a leak the guard will flag.
    """
    import sys

    sys.path.insert(0, "scripts")
    from build_public_mirror import scan_forbidden

    hits = scan_forbidden(["src/axiom/extensions/builtins/webapp"])
    assert not hits, f"consumer names leaked into the platform: {hits}"


# --- the skill (ADR-056) ----------------------------------------------------

def test_the_projection_skill_reports_what_it_projected(session):
    from axiom.extensions.builtins.webapp.skills import project

    base = datetime(2026, 9, 15, tzinfo=UTC)
    _add(session, "ch1", base)
    _add(session, "ch2", base, site="site-b")
    session.commit()

    r = project.run({"source": "signals"})
    assert r.ok, r.errors
    assert r.value["sites"] == 2
    assert r.value["channels"] == 2
    assert r.actions_taken


def test_the_projection_skill_fails_closed(session, monkeypatch):
    """A catalog that cannot refresh must say so, not report a quiet success.

    The failure it replaces is a projection that silently goes stale while the
    API keeps serving yesterday's answer with full confidence.
    """
    from axiom.extensions.builtins.webapp.catalog import sync
    from axiom.extensions.builtins.webapp.skills import project

    def _boom(*a, **k):
        raise RuntimeError("no database")

    monkeypatch.setattr(sync, "project_catalog", _boom)
    r = project.run({"source": "signals"})
    assert not r.ok
    assert any("no database" in e for e in r.errors)


# --- /series: the contract a client draws against ---------------------------
#
# The shape here is not ours to choose freely — a console reads it as
# SeriesPoint[] and groups by .channel. These tests pin the contract, because a
# field rename is invisible to the server and fatal to the chart.

def _series(session, **kw):
    from axiom.extensions.builtins.webapp.catalog import series as series_mod

    kw.setdefault("site", "site-a")
    kw.setdefault("stream", "alpha")
    kw.setdefault("bucket_s", 60)
    kw.setdefault("source", "signals")
    return series_mod.bucketed_series(session, **kw)


def test_a_point_carries_the_band_not_just_the_mean(session):
    """avg alone hides the excursion a reader is usually looking for."""
    base = datetime(2026, 9, 15, tzinfo=UTC)
    for v, m in ((10.0, 0), (30.0, 1), (20.0, 2)):
        session.execute(text(
            "INSERT INTO signals (site,stream,channel,ts,value,unit) "
            "VALUES ('site-a','alpha','ch1',:ts,:v,'degC')"),
            {"ts": (base + timedelta(seconds=m)).isoformat(), "v": v})
    session.commit()

    out = _series(session, channels=["ch1"])
    assert list(out) == ["series"], "the envelope must stay {'series': [...]}"
    p = out["series"][0]
    assert set(p) == {"channel", "ts", "avg", "min", "max", "n"}
    assert p["channel"] == "ch1"
    assert p["avg"] == 20.0 and p["min"] == 10.0 and p["max"] == 30.0
    assert p["n"] == 3


def test_channels_are_interleaved_in_one_flat_array(session):
    """The client groups by .channel; nesting would break its reader."""
    base = datetime(2026, 9, 15, tzinfo=UTC)
    for ch in ("ch1", "ch2"):
        session.execute(text(
            "INSERT INTO signals (site,stream,channel,ts,value,unit) "
            "VALUES ('site-a','alpha',:c,:ts,1.0,'degC')"),
            {"c": ch, "ts": base.isoformat()})
    session.commit()

    points = _series(session, channels=["ch1", "ch2"])["series"]
    assert isinstance(points, list)
    assert {p["channel"] for p in points} == {"ch1", "ch2"}


def test_an_empty_range_is_an_empty_array_not_an_error(session):
    """The client renders 'no data' from []; an error would be a broken page."""
    out = _series(session, channels=["nothing-here"])
    assert out == {"series": []}


def test_too_many_channels_is_refused_rather_than_truncated(session):
    """A silently dropped channel looks exactly like a channel with no data."""
    with pytest.raises(ValueError, match="at most"):
        _series(session, channels=[f"ch{i}" for i in range(20)])


def test_a_nonsense_bucket_is_refused_rather_than_defaulted(session):
    """Falling back to a default answers a question nobody asked."""
    for bad in (0, -60, "1h"):
        with pytest.raises(ValueError):
            _series(session, channels=["ch1"], bucket_s=bad)


def test_the_channels_route_shape_matches_what_the_client_reads(session):
    """stream/channel/unit/rows/first/last — all catalog-answerable."""
    base = datetime(2026, 9, 15, tzinfo=UTC)
    _add(session, "ch1", base)
    session.commit()
    sync.project_catalog(session, source="signals")

    rows = store.read_channels(session, "site-a")
    assert rows and set(rows[0]) == {"stream", "channel", "unit", "rows", "first", "last"}


# --- provenance: data that claims to be measured but cannot be --------------

def test_identical_coverage_at_two_sites_is_reported(session):
    """Two facilities do not measure identically. One of them is a copy.

    This is the shape of a real incident: one simulator's output landed for
    three sites labelled measured, and nothing objected.
    """
    from axiom.extensions.builtins.webapp.catalog import provenance_check

    base = datetime(2026, 9, 15, tzinfo=UTC)
    for site in ("site-a", "site-b"):
        for i in range(4):
            _add(session, "flow", base + timedelta(minutes=i), site=site)
    session.commit()
    sync.project_catalog(session, source="signals")

    suspects = provenance_check.find_duplicated_coverage(session)
    assert len(suspects) == 1, [s.describe() for s in suspects]
    assert suspects[0].sites == ("site-a", "site-b")
    assert "simulated" in suspects[0].describe()


def test_genuinely_different_sites_are_not_flagged(session):
    """The check must not cry wolf on ordinary multi-site data."""
    from axiom.extensions.builtins.webapp.catalog import provenance_check

    base = datetime(2026, 9, 15, tzinfo=UTC)
    for i in range(4):
        _add(session, "flow", base + timedelta(minutes=i), site="site-a")
    for i in range(7):  # different count and span
        _add(session, "flow", base + timedelta(minutes=i, seconds=30), site="site-b")
    session.commit()
    sync.project_catalog(session, source="signals")

    assert provenance_check.find_duplicated_coverage(session) == []


def test_an_empty_channel_is_not_evidence(session):
    """A channel with no rows repeats trivially and means nothing."""
    from axiom.extensions.builtins.webapp.catalog import provenance_check

    assert provenance_check.find_duplicated_coverage(session) == []


def test_a_measurement_cannot_name_the_model_that_made_it():
    """The missing half of the envelope's own rule."""
    from axiom.extensions.builtins.data_platform.daq.envelope import SignalEnvelope

    with pytest.raises(ValueError, match="not a measurement"):
        SignalEnvelope(
            producer_id="sim-1", stream="s", seq=0, prev_hash=None,
            content_hash="sha256:x", source_class="measured",
            model_ref="flowloop-sim@1.2",
        )


def test_the_existing_direction_still_holds():
    """A model that admits it must still name itself."""
    from axiom.extensions.builtins.data_platform.daq.envelope import SignalEnvelope

    with pytest.raises(ValueError, match="model_ref is required"):
        SignalEnvelope(
            producer_id="sim-1", stream="s", seq=0, prev_hash=None,
            content_hash="sha256:x", source_class="predicted",
        )


# --- site scope: one platform, several partners -----------------------------
#
# A partner's deployment must behave as though it is their site and nothing else
# exists. Before this, /api/v1/sites returned every site in the catalog to
# anyone who could reach it — on a live node that was five partners' data in one
# response.

def _client(session):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from axiom.extensions.builtins.webapp.api.routers import build_api_router

    app = FastAPI()
    app.include_router(build_api_router())
    return TestClient(app)


def _two_sites(session):
    base = datetime(2026, 9, 15, tzinfo=UTC)
    _add(session, "ch1", base, site="acu-flowloop")
    _add(session, "ch2", base, site="vcu-flowloop")
    session.commit()
    sync.project_catalog(session, source="signals")


def test_a_bounded_deployment_lists_only_its_own_site(session, monkeypatch):
    monkeypatch.setenv("AXIOM_SERVED_SITES", "acu-flowloop")
    _two_sites(session)

    body = _client(session).get("/api/v1/sites").json()
    assert [s["site"] for s in body["sites"]] == ["acu-flowloop"]


def test_another_partners_site_answers_404_not_403(session, monkeypatch):
    """403 would confirm vcu-flowloop exists on this platform."""
    monkeypatch.setenv("AXIOM_SERVED_SITES", "acu-flowloop")
    _two_sites(session)
    client = _client(session)

    assert client.get("/api/v1/sites/vcu-flowloop/channels").status_code == 404
    assert client.get("/api/v1/sites/acu-flowloop/channels").status_code == 200


def test_series_is_bounded_too_not_just_the_catalog(session, monkeypatch):
    """Every resource, not only the listing — a per-endpoint filter is the one
    that gets forgotten."""
    monkeypatch.setenv("AXIOM_SERVED_SITES", "acu-flowloop")
    _two_sites(session)

    r = _client(session).get(
        "/api/v1/sites/vcu-flowloop/series",
        params={"stream": "alpha", "channels": "ch2", "bucket_s": 60},
    )
    assert r.status_code == 404


def test_a_fleet_deployment_lists_every_site_it_serves(session, monkeypatch):
    """Fleet operators, researchers and regulators compare across sites."""
    monkeypatch.setenv("AXIOM_SERVED_SITES", "acu-flowloop,vcu-flowloop")
    _two_sites(session)

    body = _client(session).get("/api/v1/sites").json()
    assert {s["site"] for s in body["sites"]} == {"acu-flowloop", "vcu-flowloop"}


def test_an_unconfigured_deployment_still_works(session, monkeypatch):
    """A dev box with no bound must not look like a platform with no data."""
    monkeypatch.delenv("AXIOM_SERVED_SITES", raising=False)
    _two_sites(session)

    body = _client(session).get("/api/v1/sites").json()
    assert len(body["sites"]) == 2


# ---------------------------------------------------------------------------
# Retirement — the catalog must not advertise what the source no longer holds.
#
# Found live on the node: the catalog offered tamu-bubbleloop/loop.instrument
# with three channels and 978 rows each, while silver held zero rows for that
# site and stream. A partner clicking one of those gets an empty chart, which
# reads as "your data is broken" rather than "that channel is gone".
#
# The cause is that the projection only ever visited channels the source still
# had. A channel that vanished was simply never looked at, so its row survived
# every future pass.
# ---------------------------------------------------------------------------


def test_a_channel_that_leaves_the_source_leaves_the_catalog(session):
    base = datetime(2026, 9, 15, tzinfo=UTC)
    for i in range(3):
        _add(session, "keep", base + timedelta(minutes=i))
        _add(session, "vanishes", base + timedelta(minutes=i))
    session.commit()
    sync.project_catalog(session, source="signals")
    assert _rows(session, "keep") == 3
    assert _rows(session, "vanishes") == 3

    session.execute(text("DELETE FROM signals WHERE channel='vanishes'"))
    session.commit()
    sync.project_catalog(session, source="signals")

    assert _rows(session, "keep") == 3, "a live channel must survive the pass"
    assert _rows(session, "vanishes") is None, (
        "the catalog still advertises a channel the source no longer has"
    )


def test_retirement_happens_on_an_incremental_pass_too(session):
    """The channel list is a full scan even when the counts are incremental.

    Retirement must not wait for `full=True`: the nightly pass is incremental,
    and a channel that disappears would otherwise be advertised until someone
    remembered to re-baseline.
    """
    base = datetime(2026, 9, 15, tzinfo=UTC)
    _add(session, "gone", base)
    _add(session, "here", base)
    session.commit()
    sync.project_catalog(session, source="signals", full=True)
    assert _rows(session, "gone") == 1

    session.execute(text("DELETE FROM signals WHERE channel='gone'"))
    session.commit()
    sync.project_catalog(session, source="signals", full=False)
    assert _rows(session, "gone") is None
    assert _rows(session, "here") == 1


def test_an_empty_source_retires_nothing(session):
    """A source that reads as empty is a fault, not an instruction to wipe.

    If discovery returns nothing — the database is unreachable, a migration is
    mid-flight, the view is being rebuilt — the honest response is to change
    nothing. Treating "I saw no channels" as "there are no channels" would turn
    a transient fault into a wiped catalog, and the API would then report no
    data rather than stale data. Stale is recoverable; wiped looks like loss.
    """
    base = datetime(2026, 9, 15, tzinfo=UTC)
    _add(session, "ch1", base)
    session.commit()
    sync.project_catalog(session, source="signals")
    assert _rows(session, "ch1") == 1

    session.execute(text("DELETE FROM signals"))
    session.commit()
    sync.project_catalog(session, source="signals")
    assert _rows(session, "ch1") == 1, "an empty read must not empty the catalog"
