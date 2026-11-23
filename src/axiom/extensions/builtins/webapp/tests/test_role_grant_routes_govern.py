# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The routes under ``/api/v1/actors`` that grant or remove access are governing (#1889).

``POST /api/v1/actors/invite``, ``/roles``, ``/suspend`` and ``/reinstate`` give
somebody a role, change their roles, or take their access away. The web app mount
did not declare them in ``governs``, so the gate read their verb off the method,
called them ``invoke``, and a ``*:invoke`` key (a ``creator``'s grant) passed it.
After #1777 lets a key scoped to the extension that declared a route reach it, an
``actors:invoke`` key would have passed too.

Declared as governing, they carry ``http.govern`` whatever the method, so only a
scope holding ``govern`` (``*:govern``, ``webapp:govern``, ``*``) passes the gate,
and a session is left, as before, to the engine and the route's own administrator
check. The two read routes are unchanged.

These run through the composed app and the real authz middleware, because the
defect was in the mount's declaration, not in a handler.
"""

from __future__ import annotations

import dataclasses
import hashlib
import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from axiom.extensions.builtins.actors import api as actors_api
from axiom.extensions.builtins.http.authz_hook import (
    ResolvedCredential,
    _covering_scope,
    build_authz_hook,
    build_bearer_resolver,
    default_envelope_builder,
)
from axiom.extensions.builtins.http.compose import compose_app
from axiom.extensions.builtins.http.middleware import MiddlewareConfig
from axiom.extensions.builtins.http.registry import MountSpec, RouterRegistry
from axiom.extensions.builtins.webapp.mount import mount_spec
from axiom.governance import Decision, ResourceRef, Verdict
from axiom.vega.identity.principal import Principal

SITE = "example-site"
SCOPE_REFUSAL = "credential scopes do not cover this resource"

#: Every route under /api/v1/actors that grants, removes or changes a principal's
#: roles or access, with a body the handler accepts.
ROLE_GRANT_ROUTES = {
    "/api/v1/actors/invite": {"handle": "@newcomer", "role": "reader"},
    "/api/v1/actors/roles": {"email": "someone@example.org", "add": ["site-admin"]},
    "/api/v1/actors/suspend": {"email": "someone@example.org", "reason": "left"},
    "/api/v1/actors/reinstate": {"email": "someone@example.org"},
}

READ_ROUTES = ("/api/v1/actors", f"/api/v1/actors/@someone:{SITE}")


class _Keys:
    """The issued-keys store shape: token -> scopes."""

    def __init__(self, table: dict) -> None:
        self._table = table

    def resolve(self, token):
        scopes = self._table.get(token)
        if scopes is None:
            return None
        return SimpleNamespace(principal=f"@svc:{SITE}", scopes=scopes, key_id=token[-6:],
                               name="k", site=SITE)


KEYS = {
    "axk_star_invoke": ("*:invoke",),
    "axk_webapp_invoke": ("webapp:invoke",),
    "axk_creator": ("*:read", "*:invoke"),
    "axk_star_read": ("*:read",),
    "axk_webapp_read": ("webapp:read",),
    "axk_actors_invoke": ("actors:invoke",),
    "axk_actors_read": ("actors:read",),
    "axk_actors_govern": ("actors:govern",),
    "axk_star_govern": ("*:govern",),
    "axk_webapp_govern": ("webapp:govern",),
    "axk_webapp_all": ("webapp",),
    "axk_owner": ("*",),
}


def _permit(_envelope):
    return Verdict.from_decision(Decision.PERMIT, "test permits", f"t-{uuid.uuid4().hex}")


@pytest.fixture
def invoked(monkeypatch):
    """Stand the handlers' own seams in: an administrator's session claims (the
    route checks those itself) and the gate skill they call. Records each call,
    so a refusal can be shown to have happened before the handler ran."""
    calls: list[str] = []
    monkeypatch.setenv("AXIOM_SITE", SITE)
    monkeypatch.delenv("AXIOM_SERVED_SITES", raising=False)
    monkeypatch.setattr(actors_api, "_claims",
                        lambda request: {"sub": "admin", "email": "admin@example.org",
                                         "roles": ["admin"]})

    def fake_invoke(name, params, claims):
        calls.append(name)
        return SimpleNamespace(ok=True, value={"skill": name}, actions_taken=[], errors=[])

    monkeypatch.setattr(actors_api, "_invoke", fake_invoke)
    return calls


def _client(spec: MountSpec, resolve_principal=None) -> TestClient:
    registry = RouterRegistry()
    registry.register(spec)
    resolver = resolve_principal or build_bearer_resolver({}, api_keys=_Keys(KEYS))
    hook = build_authz_hook(resolve_principal=resolver, decide_fn=_permit)
    app = compose_app(registry=registry, include_builtins=False,
                      middleware=MiddlewareConfig(authz=hook), auto_authz=False)
    return TestClient(app)


@pytest.fixture
def client(invoked):
    return _client(mount_spec())


def _post(client, path, key):
    return client.post(path, json=ROLE_GRANT_ROUTES[path],
                       headers={"Authorization": f"Bearer {key}"})


def _refused_by_scope(response) -> bool:
    return response.status_code == 403 and SCOPE_REFUSAL in response.text


# ---------------------------------------------------------------- the verb


def _intent(spec: MountSpec, method: str, path: str) -> str:
    request = SimpleNamespace(method=method, url=SimpleNamespace(path=path),
                              state=SimpleNamespace(mount_extension="webapp", mount_spec=spec))
    who = Principal(handle="@someone", public_bytes=hashlib.sha256(b"@someone").digest())
    return default_envelope_builder(request, who).intent.value


@pytest.mark.parametrize("path", sorted(ROLE_GRANT_ROUTES))
def test_each_role_grant_route_carries_the_govern_verb(path):
    assert _intent(mount_spec(), "POST", path) == "http.govern"


@pytest.mark.parametrize("path", READ_ROUTES)
def test_the_actor_reads_stay_reads(path):
    assert _intent(mount_spec(), "GET", path) == "http.read"


def test_every_role_grant_route_is_a_route_the_mount_serves():
    """The declaration names routes that exist, so it cannot read as enforced and not be."""
    served = {(m, r.path) for r in mount_spec().router.routes
              for m in getattr(r, "methods", ()) or ()}
    for path in ROLE_GRANT_ROUTES:
        assert ("POST", path) in served, path


# ---------------------------------------------------------- through the gate


@pytest.mark.parametrize("key", ["axk_star_invoke", "axk_webapp_invoke", "axk_creator",
                                 "axk_star_read", "axk_actors_invoke"])
@pytest.mark.parametrize("path", sorted(ROLE_GRANT_ROUTES))
def test_a_key_without_govern_is_refused_before_the_handler(client, invoked, path, key):
    response = _post(client, path, key)
    assert _refused_by_scope(response), response.text
    assert invoked == []


@pytest.mark.parametrize("key", ["axk_star_govern", "axk_webapp_govern", "axk_webapp_all",
                                 "axk_owner"])
@pytest.mark.parametrize("path", sorted(ROLE_GRANT_ROUTES))
def test_a_key_holding_govern_reaches_the_route(client, invoked, path, key):
    response = _post(client, path, key)
    assert response.status_code == 200, response.text
    assert len(invoked) == 1


@pytest.mark.parametrize("key", ["axk_star_read", "axk_webapp_read", "axk_creator"])
@pytest.mark.parametrize("path", READ_ROUTES)
def test_a_read_key_still_reads_the_team(client, invoked, path, key):
    response = client.get(path, headers={"Authorization": f"Bearer {key}"})
    assert response.status_code == 200, response.text
    assert invoked == [("actors.list" if path == "/api/v1/actors" else "actors.show")]


@pytest.mark.parametrize("path", sorted(ROLE_GRANT_ROUTES))
def test_a_session_is_left_to_the_engine_and_the_routes_own_check(invoked, path):
    """A session carries roles, not scopes, so the gate's scope rule does not
    apply to it, exactly as before; the route's administrator check still does."""

    def session(_request):
        return ResolvedCredential(
            principal=Principal(handle="@admin:gate",
                                public_bytes=hashlib.sha256(b"@admin:gate").digest()),
            scopes=(), credential_id="session:admin",
            claims={"sub": "admin", "roles": ["admin"]}, posture="attested")

    response = _client(mount_spec(), resolve_principal=session).post(
        path, json=ROLE_GRANT_ROUTES[path])
    assert response.status_code == 200, response.text


# ------------------------------------------- with #1777's owner rule (PR #1888)


@pytest.mark.parametrize("path", sorted(ROLE_GRANT_ROUTES))
def test_an_actors_invoke_scope_cannot_cover_the_route_however_it_is_addressed(path):
    """#1777's owner rule re-addresses the resource under the extension that declared
    the route and checks the same intent. Simulated here on main's own matcher: with
    the route governing, ``actors:invoke`` covers neither address; ``actors:govern``
    covers the owner's."""
    request = SimpleNamespace(method="POST", url=SimpleNamespace(path=path),
                              state=SimpleNamespace(mount_extension="webapp",
                                                    mount_spec=mount_spec()))
    who = Principal(handle="@svc", public_bytes=hashlib.sha256(b"@svc").digest())
    envelope = default_envelope_builder(request, who)
    as_owner = ResourceRef.parse(f"extension://actors{path}")
    assert _covering_scope(("actors:invoke",), envelope.intent, envelope.resource) is None
    assert _covering_scope(("actors:invoke",), envelope.intent, as_owner) is None
    assert _covering_scope(("actors:govern",), envelope.intent, as_owner) is not None


def _owned_spec() -> MountSpec:
    if "owners" not in {f.name for f in dataclasses.fields(MountSpec)}:
        pytest.skip("MountSpec.owners is not here yet (#1777, PR #1888)")
    return dataclasses.replace(mount_spec(), owners=(("/api/v1/actors", "actors"),))


@pytest.mark.parametrize("path", sorted(ROLE_GRANT_ROUTES))
def test_under_the_owner_rule_an_actors_invoke_key_is_refused(invoked, path):
    client = _client(_owned_spec())
    assert _refused_by_scope(_post(client, path, "axk_actors_invoke"))
    assert invoked == []
    assert _post(client, path, "axk_actors_govern").status_code == 200


def test_under_the_owner_rule_an_actors_read_key_still_reads(invoked):
    client = _client(_owned_spec())
    response = client.get("/api/v1/actors", headers={"Authorization": "Bearer axk_actors_read"})
    assert response.status_code == 200, response.text
