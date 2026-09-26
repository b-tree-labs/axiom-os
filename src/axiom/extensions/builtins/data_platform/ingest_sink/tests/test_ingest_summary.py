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
    def __init__(self, context, handle="@producer:acu-flowloop"):
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
    return {"site": site, "streams": [], "total_rows": 0}


# --- the site comes from the credential -------------------------------------


def test_the_site_comes_from_the_credential_not_the_query_string():
    """The write path's rule (§5.2), inherited. A partner cannot ask about a
    site they cannot write to, because there is no parameter with which to ask."""
    calls = []
    client = _app(site="acu-flowloop", calls=calls)
    r = client.get("/ingest/summary?site=vcu-flowloop&siteName=tamu-flowloop")
    assert r.status_code == 200
    assert calls == [("acu-flowloop", None)], (
        f"a query parameter influenced the scope: {calls}"
    )
    assert r.json()["site"] == "acu-flowloop"


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
    client = _app(site="acu-flowloop")
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
            "streams": [
                {
                    "stream": "loop.instrument",
                    "schema_ref": "acu-flowloop/epics-v1",
                    "source_class": "measured",
                    "rows": 17369223,
                    "first_ts": "2026-05-06T00:00:00+00:00",
                    "last_ts": "2026-05-20T00:00:00+00:00",
                }
            ],
            "total_rows": 17369223,
            "truncated": False,
        }

    client = _app(site="acu-flowloop", summarize=summarize)
    body = client.get("/ingest/summary").json()
    assert body["total_rows"] == 17369223
    s = body["streams"][0]
    # The time range is the part that answers "is it CURRENT?", which is a
    # different question from "is it there?" and the one that matters after
    # the first week.
    assert s["first_ts"] and s["last_ts"]
    assert s["source_class"] == "measured"
    assert "note" not in body


def test_a_window_is_passed_through():
    calls = []
    client = _app(site="acu-flowloop", calls=calls)
    client.get("/ingest/summary?since_hours=24")
    assert calls == [("acu-flowloop", 24.0)]


@pytest.mark.parametrize("bad", ["0", "-1"])
def test_a_nonpositive_window_is_refused(bad):
    client = _app(site="acu-flowloop")
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
