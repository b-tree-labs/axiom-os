# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The ingest face bounds what one site can push.

It bounded a single request — items per push, rows per batch, field lengths —
and nothing at all about how many pushes. A producer in a loop, or one
draining a large backlog, was limited only by how fast it could post, against a
node whose disk was the thing that failed first.

Two properties are the point.

**The key is the site from the credential**, resolved before this runs. Keying
on anything in the body would let a caller spend another site's budget by
claiming to be them, which is the same reasoning that already stamps `site`
from the credential rather than trusting the payload.

**The budget is published on every answer, not only a refusal.** A caller that
first learns its limit by being rejected has already been rejected, and ours
paces on the published number — so a 200 carrying the remaining count is what
lets a well-behaved producer never reach the 429 at all.
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from axiom.infra.ratelimit import BucketRegistry, parse_headers  # noqa: E402

from ..api import build_tabular_ingest_router  # noqa: E402
from ..tabular import TabularIngestResult  # noqa: E402


class _Sink:
    def ingest_rows(self, source, batches):
        rows = sum(len(b.rows) for b in batches)
        return TabularIngestResult(
            source=source, accepted=len(batches), landed=len(batches),
            excluded=0, errored=0, rows_in=rows, rows_landed=rows,
            rows_duplicate=0,
        )


class _Policy:
    """Stands in for the tenancy seam: a fixed grant per client."""

    def __init__(self, site):
        self.site = site

    def grant_for(self, request):
        from ..tenancy import IngestGrant

        return IngestGrant(principal=f"@p:{self.site}", site=self.site,
                           max_access_tier=None)

    def check(self, grant, metadata):
        out = dict(metadata)
        out["site"] = grant.site
        return out


def _client(site, registry):
    app = FastAPI()
    app.include_router(
        build_tabular_ingest_router(
            _Sink(), tenancy=_Policy(site), limits=registry)
    )
    return TestClient(app)


def _body():
    return {
        "source": "epics-archive",
        "batches": [{"item_id": "i1", "schema_ref": "s/v1",
                     "rows": [{"ts": "2026-09-25T00:00:00Z", "v": 1}]}],
    }


@pytest.fixture
def clock():
    return [1000.0]


@pytest.fixture
def registry(clock):
    return BucketRegistry(capacity=3, refill_per_second=1.0, clock=lambda: clock[0])


class TestItRefusesAnOverRateCaller:
    def test_a_burst_within_the_budget_is_accepted(self, registry):
        client = _client("site-b", registry)
        for _ in range(3):
            assert client.post("/ingest/rows", json=_body()).status_code == 200

    def test_the_next_one_is_refused_with_429(self, registry):
        client = _client("site-b", registry)
        for _ in range(3):
            client.post("/ingest/rows", json=_body())
        assert client.post("/ingest/rows", json=_body()).status_code == 429

    def test_the_refusal_says_when_to_come_back(self, registry):
        client = _client("site-b", registry)
        for _ in range(3):
            client.post("/ingest/rows", json=_body())
        resp = client.post("/ingest/rows", json=_body())
        assert parse_headers(resp.headers).retry_after_s >= 1

    def test_waiting_earns_another(self, registry, clock):
        client = _client("site-b", registry)
        for _ in range(3):
            client.post("/ingest/rows", json=_body())
        clock[0] += 1.0
        assert client.post("/ingest/rows", json=_body()).status_code == 200

    def test_a_refused_request_never_reaches_the_sink(self, registry):
        """The whole point of refusing early: a caller over its budget costs
        the node nothing, which is what protects the disk."""
        class _Loud:
            def ingest_rows(self, source, batches):
                raise AssertionError("the sink was reached")

        app = FastAPI()
        app.include_router(build_tabular_ingest_router(
            _Loud(), tenancy=_Policy("site-b"), limits=registry))
        client = TestClient(app)
        for _ in range(3):
            registry.check("site-b")
        assert client.post("/ingest/rows", json=_body()).status_code == 429


class TestTheBudgetIsPerSite:
    def test_one_site_cannot_spend_anothers(self, registry):
        """The key is the site the CREDENTIAL resolved to. Keying on the body
        would let a caller drain another partner by claiming to be them."""
        b = _client("site-b", registry)
        c = _client("site-c", registry)
        for _ in range(4):
            b.post("/ingest/rows", json=_body())
        assert c.post("/ingest/rows", json=_body()).status_code == 200


class TestTheBudgetIsAlwaysPublished:
    def test_an_accepted_push_carries_the_remaining_count(self, registry):
        """This is what lets a well-behaved producer never reach the 429."""
        resp = _client("site-b", registry).post("/ingest/rows", json=_body())
        assert resp.status_code == 200
        window = parse_headers(resp.headers)
        assert window.limit == 3
        assert window.remaining == 2

    def test_the_transmitter_would_pace_on_it(self, registry):
        """Our own parser and its `should_throttle` read our own face's
        headers, which is the loop the shared module exists to close."""
        client = _client("site-b", registry)
        client.post("/ingest/rows", json=_body())
        resp = client.post("/ingest/rows", json=_body())
        window = parse_headers(resp.headers)
        assert window.should_throttle(floor=2) is True

    def test_an_accepted_push_carries_no_retry_after(self, registry):
        resp = _client("site-b", registry).post("/ingest/rows", json=_body())
        assert "retry-after" not in {k.lower() for k in resp.headers}


class TestItStillDoesEverythingElse:
    def test_the_rows_still_land(self, registry):
        resp = _client("site-b", registry).post("/ingest/rows", json=_body())
        assert resp.json()["rows_landed"] == 1

    def test_the_request_shape_is_still_bounded_first(self, registry):
        """An oversized push is a 422 regardless of budget: validating the
        shape before spending a token means a malformed request does not
        consume a good one."""
        body = _body()
        body["batches"] = body["batches"] * 10_000
        resp = _client("site-b", registry).post("/ingest/rows", json=body)
        assert resp.status_code == 422
