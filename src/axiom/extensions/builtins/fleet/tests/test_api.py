# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The /api/v1/fleet surface: fail-closed ingest auth, credential-derived
site attribution, scoped reads."""

from __future__ import annotations

import contextlib
from dataclasses import dataclass

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from axiom.extensions.builtins.fleet import api, store  # noqa: E402
from axiom.extensions.builtins.fleet.db_models import Base, FleetNode  # noqa: E402


@dataclass
class _Identity:
    principal: str
    site: str | None


class _FakeKeyStore:
    def __init__(self, tokens):
        self._tokens = tokens

    def resolve(self, token):
        return self._tokens.get(token)


@pytest.fixture()
def client(monkeypatch):
    # TestClient dispatches handlers on a worker thread; one shared
    # connection makes the in-memory DB visible across threads.
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)

    @contextlib.contextmanager
    def provider():
        session = factory()
        try:
            yield session
        finally:
            session.close()

    store.set_provider(provider)
    api.set_key_store_factory(
        lambda: _FakeKeyStore(
            {
                "tok-a": _Identity(principal="@n1:site-a", site="site-a"),
                "tok-unbound": _Identity(principal="@dev", site=None),
            }
        )
    )
    monkeypatch.delenv("AXIOM_SERVED_SITES", raising=False)

    router = fastapi.APIRouter(prefix="/api/v1")
    api.register_routes(router)
    app = fastapi.FastAPI()
    app.include_router(router)
    yield TestClient(app)
    store.reset_provider()
    api.reset_key_store_factory()
    engine.dispose()


def _body(node="n1", kind="heartbeat", payload=None):
    return {"node_id": node, "reports": [{"kind": kind, "payload": payload or {}}]}


def test_push_without_key_is_401(client):
    assert client.post("/api/v1/fleet/reports", json=_body()).status_code == 401


def test_push_with_unbound_key_is_403(client):
    r = client.post(
        "/api/v1/fleet/reports",
        json=_body(),
        headers={"Authorization": "Bearer tok-unbound"},
    )
    assert r.status_code == 403


def test_push_attributes_site_from_credential(client):
    r = client.post(
        "/api/v1/fleet/reports",
        json={**_body(), "cadences": {"heartbeat": 900}},
        headers={"Authorization": "Bearer tok-a"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["accepted"] == 1
    with store.session_scope() as s:
        assert s.get(FleetNode, "n1").site == "site-a"


def test_push_unknown_kind_is_422(client):
    r = client.post(
        "/api/v1/fleet/reports",
        json=_body(kind="weather"),
        headers={"Authorization": "Bearer tok-a"},
    )
    assert r.status_code == 422


def test_status_returns_evidence(client):
    client.post(
        "/api/v1/fleet/reports",
        json=_body(),
        headers={"Authorization": "Bearer tok-a"},
    )
    r = client.get("/api/v1/fleet/status")
    assert r.status_code == 200
    (node,) = r.json()["nodes"]
    assert node["rollup"] == "green"
    assert node["kinds"]["heartbeat"]["evidence"]


def test_status_with_key_narrows_to_its_site(client):
    client.post(
        "/api/v1/fleet/reports",
        json=_body(),
        headers={"Authorization": "Bearer tok-a"},
    )
    r = client.get("/api/v1/fleet/status", headers={"Authorization": "Bearer tok-a"})
    assert [n["site"] for n in r.json()["nodes"]] == ["site-a"]


# --- ADR-123 D3: reads ride the webgate cookie session too --------------


@dataclass
class _SessionCred:
    claims: dict


def _install_session(cookie_to_claims):
    """Fake the session leg of chain_resolvers(session, bearer)."""

    def resolver(request):
        cookie = request.cookies.get("axiom_session")
        claims = cookie_to_claims.get(cookie)
        return _SessionCred(claims=claims) if claims else None

    api.set_session_resolver_factory(lambda: resolver)


@pytest.fixture()
def session_client(client):
    _install_session(
        {
            "sess-a": {"sub": "ben", "site": "site-a"},
            "sess-nosite": {"sub": "ben"},
        }
    )
    yield client
    api.reset_session_resolver_factory()


def _seed(client):
    client.post(
        "/api/v1/fleet/reports",
        json=_body(),
        headers={"Authorization": "Bearer tok-a"},
    )


def test_status_with_session_cookie_narrows_to_claimed_site(session_client):
    _seed(session_client)
    r = session_client.get("/api/v1/fleet/status", cookies={"axiom_session": "sess-a"})
    assert r.status_code == 200
    assert [n["site"] for n in r.json()["nodes"]] == ["site-a"]


def test_session_site_outside_scope_is_404_never_403(session_client, monkeypatch):
    _seed(session_client)
    monkeypatch.setenv("AXIOM_SERVED_SITES", "site-b")
    r = session_client.get("/api/v1/fleet/status", cookies={"axiom_session": "sess-a"})
    assert r.status_code == 404


def test_session_without_site_claim_keeps_deployment_scope(session_client, monkeypatch):
    _seed(session_client)
    monkeypatch.setenv("AXIOM_SERVED_SITES", "site-a")
    r = session_client.get("/api/v1/fleet/status", cookies={"axiom_session": "sess-nosite"})
    assert r.status_code == 200
    assert [n["site"] for n in r.json()["nodes"]] == ["site-a"]


def test_session_resolves_before_bearer_per_hook_order(session_client):
    # chain_resolvers(session, bearer): when both are presented, the
    # session leg answers first (same order as the gate's front door).
    _seed(session_client)
    r = session_client.get(
        "/api/v1/fleet/status",
        headers={"Authorization": "Bearer tok-a"},
        cookies={"axiom_session": "sess-a"},
    )
    assert r.status_code == 200
    assert [n["site"] for n in r.json()["nodes"]] == ["site-a"]


def test_ingest_never_accepts_a_session_cookie(session_client):
    # Writes stay bearer-only (ADR-123 D3): a browser session cannot push.
    r = session_client.post(
        "/api/v1/fleet/reports",
        json=_body(),
        cookies={"axiom_session": "sess-a"},
    )
    assert r.status_code == 401
