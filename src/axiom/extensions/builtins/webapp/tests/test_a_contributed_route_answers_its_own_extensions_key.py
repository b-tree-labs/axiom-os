# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A route an extension contributes to ``/api/v1`` answers a key scoped to that extension (#1777).

Found in a node smoke test: a node's fleet push was refused every few minutes for
over a week. Its key was scoped to ``fleet``, and ``fleet`` declares its routes as
``kind = "api"``, which mounts them inside the web app's ``/api/v1``. The gate
addressed every such request as ``extension://webapp/...``, so no scope naming the
extension that declares the route could ever cover it.

The fix is additive: a scope naming the declaring extension covers that
extension's own subpath and nothing else. These run through the composed app and
the real authz middleware, and most of them prove what did NOT change: a key for
one extension reaches no other extension's routes and none of the web app's own,
the verb still has to match, a subpath that overlaps the web app's own routes is
owned by nobody, and no credential is still no credential.
"""

from __future__ import annotations

import contextlib
import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from axiom.extensions.builtins.fleet import api as fleet_api
from axiom.extensions.builtins.fleet import store as fleet_store
from axiom.extensions.builtins.fleet.db_models import Base as FleetBase
from axiom.extensions.builtins.http.authz_hook import build_authz_hook, build_bearer_resolver
from axiom.extensions.builtins.http.compose import compose_app
from axiom.extensions.builtins.http.middleware import MiddlewareConfig
from axiom.extensions.builtins.http.registry import RouterRegistry
from axiom.extensions.builtins.webapp.mount import mount_spec
from axiom.governance import Decision, Verdict

SITE = "example-site"
SCOPE_REFUSAL = "credential scopes do not cover this resource"


class _Keys:
    """The issued-keys store shape: token -> (principal, scopes, site)."""

    def __init__(self, table: dict) -> None:
        self._table = table

    def resolve(self, token):
        found = self._table.get(token)
        if found is None:
            return None
        principal, scopes, site = found
        return SimpleNamespace(principal=principal, scopes=scopes, key_id=token[-6:],
                               name="k", site=site)


KEYS = {
    "axk_fleet_invoke": ("@node:example-site", ("fleet:invoke",), SITE),
    "axk_fleet_all": ("@node:example-site", ("fleet",), SITE),
    "axk_fleet_read": ("@node:example-site", ("fleet:read",), SITE),
    "axk_receipts_read": ("@svc:example-site", ("receipts:read",), SITE),
    "axk_sites_read": ("@svc:example-site", ("sites:read",), SITE),
    "axk_unrelated": ("@svc:example-site", ("unrelated",), SITE),
}


def _permit(_envelope):
    return Verdict.from_decision(Decision.PERMIT, "test permits", f"t-{uuid.uuid4().hex}")


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("AXIOM_SITE", SITE)
    monkeypatch.delenv("AXIOM_SERVED_SITES", raising=False)
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    FleetBase.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)

    @contextlib.contextmanager
    def provider():
        session = factory()
        try:
            yield session
        finally:
            session.close()

    keys = _Keys(KEYS)
    fleet_store.set_provider(provider)
    fleet_api.set_key_store_factory(lambda: keys)
    registry = RouterRegistry()
    registry.register(mount_spec())
    hook = build_authz_hook(resolve_principal=build_bearer_resolver({}, api_keys=keys),
                            decide_fn=_permit)
    app = compose_app(registry=registry, include_builtins=False,
                      middleware=MiddlewareConfig(authz=hook), auto_authz=False)
    yield TestClient(app)
    fleet_store.reset_provider()
    fleet_api.reset_key_store_factory()
    engine.dispose()


def _push(client, key):
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    return client.post("/api/v1/fleet/reports", headers=headers,
                       json={"node_id": "n1", "reports": [{"kind": "heartbeat", "payload": {}}]})


def _refused_by_scope(response) -> bool:
    return response.status_code == 403 and SCOPE_REFUSAL in response.text


@pytest.mark.parametrize("key", ["axk_fleet_invoke", "axk_fleet_all"])
def test_a_key_scoped_to_the_extension_pushes_to_its_route(client, key):
    response = _push(client, key)
    assert response.status_code == 200, response.text
    assert response.json()["accepted"] == 1


def test_the_verb_still_has_to_match(client):
    assert _refused_by_scope(_push(client, "axk_fleet_read"))


def test_a_key_for_one_extension_does_not_reach_another_extensions_route(client):
    assert _refused_by_scope(_push(client, "axk_receipts_read"))
    assert _refused_by_scope(_push(client, "axk_unrelated"))
    response = client.get("/api/v1/receipts/today",
                          headers={"Authorization": "Bearer axk_fleet_all"})
    assert _refused_by_scope(response), response.text


@pytest.mark.parametrize("path", ["/api/v1/sites", f"/api/v1/sites/{SITE}/channels",
                                  "/api/v1/binding", "/api/v1/views/home"])
def test_a_key_for_an_extension_does_not_reach_the_web_apps_own_routes(client, path):
    response = client.get(path, headers={"Authorization": "Bearer axk_fleet_all"})
    assert _refused_by_scope(response), response.text


def test_a_subpath_that_overlaps_the_web_apps_own_routes_is_owned_by_nobody(client):
    # The `sites` extension contributes `/sites`, which the web app's own catalog
    # also serves. A `sites` key must not reach the catalog through that overlap.
    response = client.get(f"/api/v1/sites/{SITE}/channels",
                          headers={"Authorization": "Bearer axk_sites_read"})
    assert _refused_by_scope(response), response.text


def test_no_credential_is_still_refused(client):
    response = _push(client, None)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "invalid_credential"


def test_an_unknown_key_is_still_refused(client):
    response = _push(client, "axk_not_issued")
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "invalid_credential"
