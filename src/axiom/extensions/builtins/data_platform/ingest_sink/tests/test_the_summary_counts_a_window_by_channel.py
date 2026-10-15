# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``/ingest/summary?by=channel`` — so a producer can prove N sent is N landed.

The summary answered "how many rows has this feed got", which cannot tell a
producer that one channel of forty stopped arriving, or that the last hour of a
run is missing. A producer knows exactly what it sent per channel and per
window; the face has to be able to answer in the same terms for the two to be
compared.

The scoping rule does not change: the site comes from the credential. The new
parameters narrow WITHIN that site and can never widen past it.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from axiom.extensions.builtins.data_platform.ingest_sink.summary import (
    MAX_CHANNEL_WINDOW_DAYS,
    build_ingest_summary_router,
)
from axiom.extensions.builtins.data_platform.ingest_sink.tenancy import TenancyPolicy


class _Principal:
    def __init__(self, context, handle="@producer:site-b"):
        self.context = context
        self.handle = handle


def _app(*, site, calls):
    def count_channels(site, feed, start, end):
        calls.append((site, feed, start.isoformat(), end.isoformat()))
        return [
            {"channel": "STC1", "rows": 5, "first_ts": None, "last_ts": None},
            {"channel": "PTC1", "rows": 4, "first_ts": None, "last_ts": None},
        ]

    app = FastAPI()
    app.include_router(
        build_ingest_summary_router(
            tenancy=TenancyPolicy.from_env(),
            summarize=lambda s, h: {"site": s, "feeds": [], "total_rows": 0},
            count_channels=count_channels,
        )
    )

    @app.middleware("http")
    async def _principal(request, call_next):
        request.state.principal = _Principal(site) if site else None
        return await call_next(request)

    return TestClient(app)


Q = "/ingest/summary?by=channel&feed=loop.runs&from=2026-09-24T15:00:00Z&to=2026-09-24T16:00:00Z"


def test_counts_come_back_per_channel_for_the_window():
    calls = []
    r = _app(site="site-b", calls=calls).get(Q)
    assert r.status_code == 200
    body = r.json()
    assert body["by"] == "channel"
    assert body["feed"] == "loop.runs"
    assert {c["channel"]: c["rows"] for c in body["channels"]} == {"STC1": 5, "PTC1": 4}
    assert body["total_rows"] == 9
    assert calls == [
        ("site-b", "loop.runs", "2026-09-24T15:00:00+00:00", "2026-09-24T16:00:00+00:00")
    ]


def test_the_site_still_comes_from_the_credential():
    calls = []
    r = _app(site="site-b", calls=calls).get(Q + "&site=site-c")
    assert r.status_code == 200
    assert calls[0][0] == "site-b"


def test_no_site_on_the_credential_is_still_refused():
    r = _app(site=None, calls=[]).get(Q)
    assert r.status_code == 403


def test_a_channel_count_needs_a_feed_and_a_window():
    client = _app(site="site-b", calls=[])
    assert (
        client.get(
            "/ingest/summary?by=channel&from=2026-09-24T15:00:00Z&to=2026-09-24T16:00:00Z"
        ).status_code
        == 422
    )
    assert (
        client.get("/ingest/summary?by=channel&feed=f&to=2026-09-24T16:00:00Z").status_code == 422
    )
    assert (
        client.get("/ingest/summary?by=channel&feed=f&from=2026-09-24T15:00:00Z").status_code == 422
    )


def test_an_unparseable_or_backwards_window_is_refused():
    client = _app(site="site-b", calls=[])
    assert (
        client.get(
            "/ingest/summary?by=channel&feed=f&from=yesterday&to=2026-09-24T16:00:00Z"
        ).status_code
        == 422
    )
    r = client.get(
        "/ingest/summary?by=channel&feed=f&from=2026-09-24T16:00:00Z&to=2026-09-24T15:00:00Z"
    )
    assert r.status_code == 422


def test_a_window_too_long_to_count_cheaply_is_refused_with_the_limit():
    r = _app(site="site-b", calls=[]).get(
        "/ingest/summary?by=channel&feed=f&from=2026-01-01T00:00:00Z&to=2026-09-24T00:00:00Z"
    )
    assert r.status_code == 422
    assert str(MAX_CHANNEL_WINDOW_DAYS) in r.json()["detail"]


def test_an_unknown_grouping_is_refused_rather_than_ignored():
    r = _app(site="site-b", calls=[]).get("/ingest/summary?by=schema")
    assert r.status_code == 422


def test_a_naive_timestamp_is_read_as_utc():
    calls = []
    _app(site="site-b", calls=calls).get(
        "/ingest/summary?by=channel&feed=f&from=2026-09-24T15:00:00&to=2026-09-24T16:00:00"
    )
    assert calls[0][2] == "2026-09-24T15:00:00+00:00"


def test_the_ingest_app_passes_the_channel_count_through(tmp_path):
    """The app factory mounts the summary router; a count it could not be
    given would leave by=channel reaching for a database in every harness."""
    from axiom.extensions.builtins.data_platform.agents.plinth.connectors import (
        ConnectorConfig,
    )
    from axiom.extensions.builtins.data_platform.ingest_sink import (
        build_tabular_writer_for_config,
    )
    from axiom.extensions.builtins.data_platform.ingest_sink.api import (
        create_tabular_ingest_app,
    )
    from axiom.extensions.builtins.data_platform.ingest_sink.tabular import (
        TabularIngestSink,
    )

    seen = []

    def count_channels(site, feed, start, end):
        seen.append(feed)
        return [{"channel": "STC1", "rows": 1, "first_ts": None, "last_ts": None}]

    writer = build_tabular_writer_for_config(
        ConnectorConfig(
            name="loop",
            kind="push",
            bronze_root=str(tmp_path),
            site="site-b",
            default_disposition="allow",
        )
    )
    app = create_tabular_ingest_app(TabularIngestSink(writer=writer), count_channels=count_channels)

    @app.middleware("http")
    async def _principal(request, call_next):
        request.state.principal = _Principal("site-b")
        return await call_next(request)

    assert TestClient(app).get(Q).json()["total_rows"] == 1
    assert seen == ["loop.runs"]
