# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The face was write-only, so "accepted" was the only answer a producer got.

A batch can be accepted, written to bronze, and absent from silver because no
normalizer claimed its schema_ref. The producer's view is an HTTP status from
hours ago. For a tenant-posture partner — a face URL, a site key, no database —
"is my data there?" could only be answered by someone on our side running SQL.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from axiom.extensions.builtins.data_platform.ingest_sink.summary import (
    build_ingest_summary_router,
)
from axiom.extensions.builtins.data_platform.ingest_sink.tenancy import TenancyPolicy


class _Principal:
    def __init__(self, context, handle="@producer:site-b"):
        self.context = context
        self.handle = handle


def _app(*, site: str | None, summarize=None, calls=None):
    """Mount the router behind a middleware that sets the principal, the way
    the real gate does."""
    app = FastAPI()
    app.include_router(
        build_ingest_summary_router(
            tenancy=TenancyPolicy.from_env(),
            summarize=summarize or (lambda s, h: _record(calls, s, h)),
        )
    )

    @app.middleware("http")
    async def _principal(request, call_next):
        request.state.principal = _Principal(site) if site else None
        return await call_next(request)

    return TestClient(app)


def _record(calls, site, hours):
    if calls is not None:
        calls.append((site, hours))
    return {"site": site, "feeds": [], "total_rows": 0}


# --- the site comes from the credential -------------------------------------


def test_the_site_comes_from_the_credential_not_the_query_string():
    """The write path's rule (§5.2), inherited. A partner cannot ask about a
    site they cannot write to, because there is no parameter with which to ask."""
    calls = []
    client = _app(site="site-b", calls=calls)
    r = client.get("/ingest/summary?site=site-c&siteName=site-d")
    assert r.status_code == 200
    assert calls == [("site-b", None)], (
        f"a query parameter influenced the scope: {calls}"
    )
    assert r.json()["site"] == "site-b"


def test_a_credential_with_no_site_is_refused_not_given_everything():
    """An un-gated mount answering 'all sites' is a different endpoint than the
    one this is meant to be."""
    client = _app(site=None)
    r = client.get("/ingest/summary")
    assert r.status_code == 403
    assert "scoped to the site" in r.json()["detail"]


# --- the answer is useful ----------------------------------------------------


def test_zero_rows_explains_that_accepted_is_not_landed():
    """A producer posting 2xx all morning needs to be told the difference."""
    client = _app(site="site-b")
    body = client.get("/ingest/summary").json()
    assert body["total_rows"] == 0
    note = body["note"]
    assert "accepted, not" in note
    assert "bronze and stops there" in note, (
        "the note must name the actual failure mode — an unclaimed schema_ref "
        "— or it is just a friendlier zero"
    )


def test_rows_are_reported_per_stream_with_a_time_range():
    def summarize(site, hours):
        return {
            "site": site,
            "feeds": [
                {
                    "feed": "loop.instrument",
                    "schema_ref": "site-b/epics-v1",
                    "source_class": "measured",
                    "rows": 17369223,
                    "first_ts": "2026-05-06T00:00:00+00:00",
                    "last_ts": "2026-05-20T00:00:00+00:00",
                }
            ],
            "total_rows": 17369223,
            "truncated": False,
        }

    client = _app(site="site-b", summarize=summarize)
    body = client.get("/ingest/summary").json()
    assert body["total_rows"] == 17369223
    s = body["feeds"][0]
    # The time range is the part that answers "is it CURRENT?", which is a
    # different question from "is it there?" and the one that matters after
    # the first week.
    assert s["first_ts"] and s["last_ts"]
    assert s["source_class"] == "measured"
    assert "note" not in body


def test_a_window_is_passed_through():
    calls = []
    client = _app(site="site-b", calls=calls)
    client.get("/ingest/summary?since_hours=24")
    assert calls == [("site-b", 24.0)]


@pytest.mark.parametrize("bad", ["0", "-1"])
def test_a_nonpositive_window_is_refused(bad):
    client = _app(site="site-b")
    assert client.get(f"/ingest/summary?since_hours={bad}").status_code == 422


def test_the_query_filters_by_site_first():
    """site is the leading column of the only usable index on silver.signals.
    A summary that reaches for schema_ref instead is a sequential scan over
    tens of millions of rows — that mistake cost a 150s timeout once already."""
    import inspect

    from axiom.extensions.builtins.data_platform.ingest_sink import summary

    src = inspect.getsource(summary._default_summarize)
    where = src[src.index("where ="):src.index("sql = text")]
    assert "site = :site" in where
    assert where.index("site = :site") < where.find("schema_ref") % (len(where) + 1)


# --- it has to be MOUNTED, not merely written -------------------------------


class TestTheRouteExistsOnTheAppTheFactoryBuilds:
    """The gap every test above missed by construction.

    Each of them mounts the router by hand, so all of them passed while the
    only references to `build_ingest_summary_router` outside this module were
    its own `__all__` and these tests. `GET /ingest/summary` existed in the
    tree and on no running app.

    The cost landed on the last rung of a partner's walk. `neut daq verify`
    asks this endpoint whether rows actually landed, and against any face the
    factory built it got a 404 — which that verb explains by saying the face
    "predates the read side", sending a partner to upgrade a node that was
    already current. A unit test that builds its own app cannot catch that; it
    has to ask the factory.
    """

    @staticmethod
    def _client(*, site="site-b", summarize=None, calls=None):
        from axiom.extensions.builtins.data_platform.ingest_sink.api import (
            create_tabular_ingest_app,
        )
        from axiom.extensions.builtins.data_platform.ingest_sink.tabular import (
            TabularIngestSink,
        )

        class _Writer:
            def write_rows(self, *a, **k):
                return {"written": 0}

        app = create_tabular_ingest_app(
            TabularIngestSink(writer=_Writer()),
            summarize=summarize or (lambda s, h: _record(calls, s, h)),
        )

        @app.middleware("http")
        async def _principal(request, call_next):
            request.state.principal = _Principal(site) if site else None
            return await call_next(request)

        return TestClient(app, raise_server_exceptions=False)

    def test_the_summary_route_is_not_a_404(self):
        """The regression guard. Anything other than 404 means it is mounted;
        which status it is depends on the credential and is covered above."""
        assert self._client(site=None).get("/ingest/summary").status_code != 404

    def test_a_scoped_credential_gets_an_answer(self):
        response = self._client(site="site-b").get("/ingest/summary")
        assert response.status_code == 200, response.text
        assert response.json()["site"] == "site-b"

    def test_an_unscoped_credential_still_gets_403_not_404(self):
        """Mounting it must not have loosened it: no site on the credential is
        no scope, and answering "everything" would make an un-gated mount a
        cross-site read."""
        assert self._client(site=None).get("/ingest/summary").status_code == 403

    def test_the_write_lane_is_still_there(self):
        """Mounting a second router must not displace the first."""
        response = self._client().post("/ingest/rows", json={})
        assert response.status_code != 404

    def test_the_factory_passes_the_query_through(self):
        """So a deployment supplies its own read and the endpoint keeps doing
        only tenancy and shape."""
        calls: list[tuple] = []
        self._client(site="site-b", calls=calls).get("/ingest/summary?since_hours=3")
        assert calls == [("site-b", 3.0)]

    def test_the_default_factory_needs_no_summarize_argument(self):
        """A caller that passes nothing still gets the route, because the
        router falls back to its own query. Otherwise this fix would only work
        for callers who knew to ask for it — which is the shape of the bug."""
        from axiom.extensions.builtins.data_platform.ingest_sink.api import (
            create_tabular_ingest_app,
        )
        from axiom.extensions.builtins.data_platform.ingest_sink.tabular import (
            TabularIngestSink,
        )

        class _Writer:
            def write_rows(self, *a, **k):
                return {"written": 0}

        app = create_tabular_ingest_app(TabularIngestSink(writer=_Writer()))
        client = TestClient(app, raise_server_exceptions=False)
        assert client.get("/ingest/summary").status_code != 404
