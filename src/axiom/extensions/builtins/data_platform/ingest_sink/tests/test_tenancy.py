# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tenancy at the ingest face (ADR-106; spec-signal-ingest-and-producer §5.2).

Site and tier ceiling come from the credential. A payload asserting another
site is 403, a tier above the ceiling is 403, an unknown tier is 422, and the
credential's site is stamped on what lands. Both lanes, same rule.
"""

from __future__ import annotations

import pytest

from ...ingest_sink import TabularIngestResult
from ...ingest_sink.tenancy import (
    DEFAULT_TIER_LADDER,
    IngestGrant,
    TenancyPolicy,
    TenancyRefused,
    tier_ladder,
)

# ---------------------------------------------------------------- policy


def test_ladder_defaults_and_env_override():
    assert tier_ladder({}) == DEFAULT_TIER_LADDER
    assert tier_ladder({"AXIOM_ACCESS_TIER_LADDER": "public, internal ,restricted"}) == (
        "public",
        "internal",
        "restricted",
    )


def test_from_env_parses_ceilings_and_refuses_off_ladder_ones():
    pol = TenancyPolicy.from_env(
        {
            "AXIOM_INGEST_MAX_TIER": "public",
            "AXIOM_INGEST_SITE_MAX_TIERS": "alpha=restricted, beta=public",
        }
    )
    assert pol.default_max_tier == "public"
    assert dict(pol.site_max_tiers) == {"alpha": "restricted", "beta": "public"}
    with pytest.raises(ValueError, match="not on the tier ladder"):
        TenancyPolicy.from_env({"AXIOM_INGEST_MAX_TIER": "galactic"})
    with pytest.raises(ValueError, match="site=tier"):
        TenancyPolicy.from_env({"AXIOM_INGEST_SITE_MAX_TIERS": "alpha"})


class _State:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _Req:
    def __init__(self, principal=None, actor=None):
        self.state = _State(principal=principal, actor=actor)


def _principal(handle: str):
    from axiom.vega.identity.principal import Principal

    return Principal(handle=handle, public_bytes=b"\x00" * 32)


def test_grant_comes_from_the_principal_context_then_the_actor_tenant():
    pol = TenancyPolicy(default_max_tier="public", site_max_tiers={"alpha": "restricted"})
    g = pol.grant_for(_Req(principal=_principal("@daq:alpha")))
    assert g == IngestGrant(principal="@daq:alpha", site="alpha", max_access_tier="restricted")
    # a site without a specific ceiling gets the default
    assert pol.grant_for(_Req(principal=_principal("@daq:beta"))).max_access_tier == "public"
    # no context on the handle → the actor's tenant, when present
    g = pol.grant_for(_Req(principal=_principal("@daq"), actor=_State(tenant="alpha")))
    assert g.site == "alpha" and g.max_access_tier == "restricted"
    # nothing resolved → siteless grant with only the default ceiling
    assert pol.grant_for(_Req()) == IngestGrant(principal=None, site=None, max_access_tier="public")
    assert TenancyPolicy().grant_for(object()) == IngestGrant(None, None, None)


def test_check_refuses_a_foreign_site_and_stamps_the_credentials_site():
    pol = TenancyPolicy()
    grant = IngestGrant(principal="@daq:alpha", site="alpha", max_access_tier=None)
    assert pol.check(grant, {}) == {"site": "alpha"}
    assert pol.check(grant, {"site": "alpha", "k": "v"}) == {"site": "alpha", "k": "v"}
    with pytest.raises(TenancyRefused) as ei:
        pol.check(grant, {"site": "beta"})
    assert ei.value.status == 403 and "not the credential's site" in ei.value.detail
    # a siteless credential may not assert a site either — site is never payload-derived
    with pytest.raises(TenancyRefused) as ei:
        pol.check(IngestGrant(None, None, None), {"site": "alpha"})
    assert ei.value.status == 403
    # and a siteless credential stamps nothing
    assert pol.check(IngestGrant(None, None, None), {"k": "v"}) == {"k": "v"}


def test_check_enforces_the_tier_ceiling():
    pol = TenancyPolicy()
    capped = IngestGrant("@daq:alpha", "alpha", "restricted")
    assert pol.check(capped, {"access_tier": "public"})["access_tier"] == "public"
    assert pol.check(capped, {"access_tier": "restricted"})["access_tier"] == "restricted"
    with pytest.raises(TenancyRefused) as ei:
        pol.check(capped, {"access_tier": "export_controlled"})
    assert ei.value.status == 403 and "above the credential's ceiling" in ei.value.detail
    with pytest.raises(TenancyRefused) as ei:
        pol.check(capped, {"access_tier": "galactic"})
    assert ei.value.status == 422
    # no tier grant at all → a request may not ask for a tier
    with pytest.raises(TenancyRefused) as ei:
        pol.check(IngestGrant("@daq:alpha", "alpha", None), {"access_tier": "public"})
    assert ei.value.status == 403 and "no tier grant" in ei.value.detail


# ---------------------------------------------------------------- the face


class _CaptureRows:
    """A row sink that records what the face handed it."""

    def __init__(self):
        self.calls = []

    def ingest_rows(self, source, batches, *, run_store=None):
        batches = list(batches)
        self.calls.append((source, batches))
        return TabularIngestResult(
            source=source,
            accepted=len(batches),
            landed=len(batches),
            excluded=0,
            errored=0,
            rows_in=sum(len(b.rows) for b in batches),
            rows_landed=sum(len(b.rows) for b in batches),
            rows_duplicate=0,
        )


class _CaptureDocs:
    def __init__(self):
        self.calls = []

    def ingest(self, source, items, *, run_store=None):
        from dataclasses import fields

        from ...ingest_sink import IngestResult

        items = list(items)
        self.calls.append((source, items))
        # shape-agnostic: every counter 0, then the three that matter
        kw = {f.name: [] if f.name == "items" else 0 for f in fields(IngestResult)}
        kw.update(source=source, accepted=len(items), landed=len(items))
        return IngestResult(**kw)


def _client(handle: str | None, policy: TenancyPolicy, rows: _CaptureRows, docs: _CaptureDocs):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from axiom.extensions.builtins.http.server import create_app

    from ...ingest_sink.api import build_ingest_router, build_tabular_ingest_router

    app = create_app(title="t", version="0", description="")

    @app.middleware("http")
    async def _inject(request, call_next):
        # stand-in for the authz seam (SRV-022), which sets request.state.principal
        if handle:
            request.state.principal = _principal(handle)
        return await call_next(request)

    app.include_router(build_tabular_ingest_router(sink=rows, tenancy=policy))
    app.include_router(build_ingest_router(sink=docs, tenancy=policy))
    return TestClient(app)


def _rows_body(**metadata):
    return {
        "source": "unit-src",
        "batches": [
            {"item_id": "b1", "schema_ref": "t/v1", "rows": [{"a": 1}], "metadata": metadata}
        ],
    }


def _docs_body(**metadata):
    return {
        "source": "unit-src",
        "items": [{"item_id": "d1", "content": "x", "metadata": metadata}],
    }


@pytest.fixture
def policy():
    return TenancyPolicy(default_max_tier="public", site_max_tiers={"alpha": "restricted"})


def test_rows_lane_stamps_the_site_and_refuses_a_foreign_one(policy):
    rows, docs = _CaptureRows(), _CaptureDocs()
    c = _client("@daq:alpha", policy, rows, docs)
    assert c.post("/ingest/rows", json=_rows_body(k="v")).status_code == 200
    (_, [batch]) = rows.calls[-1]
    assert batch.metadata == {"k": "v", "site": "alpha"}
    r = c.post("/ingest/rows", json=_rows_body(site="beta"))
    assert r.status_code == 403 and "not the credential's site" in r.json()["detail"]
    assert len(rows.calls) == 1  # refused before the sink was touched


def test_rows_lane_enforces_the_ceiling_before_any_write(policy):
    rows, docs = _CaptureRows(), _CaptureDocs()
    c = _client("@daq:alpha", policy, rows, docs)
    assert c.post("/ingest/rows", json=_rows_body(access_tier="restricted")).status_code == 200
    assert rows.calls[-1][1][0].metadata["access_tier"] == "restricted"
    r = c.post("/ingest/rows", json=_rows_body(access_tier="export_controlled"))
    assert r.status_code == 403 and "ceiling" in r.json()["detail"]
    assert c.post("/ingest/rows", json=_rows_body(access_tier="galactic")).status_code == 422
    # a site under the default ceiling only
    c2 = _client("@daq:beta", policy, rows, docs)
    assert c2.post("/ingest/rows", json=_rows_body(access_tier="restricted")).status_code == 403
    assert len(rows.calls) == 1


def test_document_lane_applies_the_same_rule(policy):
    rows, docs = _CaptureRows(), _CaptureDocs()
    c = _client("@daq:alpha", policy, rows, docs)
    assert c.post("/ingest", json=_docs_body()).status_code == 200
    (_, [item]) = docs.calls[-1]
    assert item.metadata == {"site": "alpha"}
    assert c.post("/ingest", json=_docs_body(site="beta")).status_code == 403
    assert c.post("/ingest", json=_docs_body(access_tier="export_controlled")).status_code == 403
    assert len(docs.calls) == 1


def test_no_principal_may_neither_assert_a_site_nor_request_a_tier():
    rows, docs = _CaptureRows(), _CaptureDocs()
    c = _client(None, TenancyPolicy(), rows, docs)
    assert c.post("/ingest/rows", json=_rows_body(k="v")).status_code == 200
    assert rows.calls[-1][1][0].metadata == {"k": "v"}  # nothing stamped, nothing refused
    assert c.post("/ingest/rows", json=_rows_body(site="alpha")).status_code == 403
    assert c.post("/ingest/rows", json=_rows_body(access_tier="public")).status_code == 403


def test_default_policy_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv("AXIOM_INGEST_MAX_TIER", "public")
    monkeypatch.setenv("AXIOM_INGEST_SITE_MAX_TIERS", "alpha=restricted")
    rows = _CaptureRows()
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from axiom.extensions.builtins.http.server import create_app

    from ...ingest_sink.api import build_tabular_ingest_router

    app = create_app(title="t", version="0", description="")

    @app.middleware("http")
    async def _inject(request, call_next):
        request.state.principal = _principal("@daq:alpha")
        return await call_next(request)

    app.include_router(build_tabular_ingest_router(sink=rows))  # tenancy omitted → env
    c = TestClient(app)
    assert c.post("/ingest/rows", json=_rows_body(access_tier="restricted")).status_code == 200
    assert (
        c.post("/ingest/rows", json=_rows_body(access_tier="export_controlled")).status_code == 403
    )
