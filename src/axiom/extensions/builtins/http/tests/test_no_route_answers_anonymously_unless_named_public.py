# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""No mounted route answers a request without a credential unless it is named public here.

``/api/v1`` once turned its mount gate off and left authentication to each route.
The catalog routes never checked, so a site node listed every hosted site's
channels, row counts and date ranges to anyone who could reach its port
(2026-10-30). The class of defect is "a route that forgets", and a per-route
test cannot catch it: the forgotten route is the one nobody wrote a test for.

So this walks every route of every mount the platform ships (the built-in
factories and every built-in manifest's ``service`` mount), asks the
middleware's own rule whether a credential is required, and fails on any route
that is answerable anonymously and is not listed below with its reason. Then it
sends every gated route a request with no credential and checks the gate
refuses it before the handler runs.

Adding a public route means adding it here, in review, with a reason.
"""

from __future__ import annotations

import re
import tomllib
import uuid
from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from axiom.extensions.builtins.http.authz_hook import build_authz_hook, build_bearer_resolver
from axiom.extensions.builtins.http.compose import compose_app
from axiom.extensions.builtins.http.middleware import MiddlewareConfig, requires_credential
from axiom.extensions.builtins.http.registry import RouterRegistry
from axiom.governance import Decision, Verdict

BUILTINS = Path(__file__).resolve().parents[2]

#: Mounts that are public throughout (``requires_authz=False``), and why.
PUBLIC_MOUNTS = {
    "/.well-known": "OAuth discovery and the public JWKS: a client must read them to sign in",
    "/oauth": "the OAuth protocol endpoints; the client authenticates inside the protocol",
    "/gate": "sign-in, redeem and reset; webgate authenticates each route that needs it",
    "/healthz": "liveness for a load balancer; says only that the process is up",
    "/approve": "a signed one-time approval link is itself the credential",
    "/releases": "signed release manifests; a node trusts the signature, not the host",
}

#: Single routes on a gated mount that answer without a credential, and why.
PUBLIC_PATHS = {
    "/api/v1/health": "liveness; names no site, person or key",
    "/api/v1/version": "build identity, so a bound client can compare versions",
}


def _manifests() -> list[dict]:
    return [tomllib.loads(p.read_text()) for p in sorted(BUILTINS.glob("*/axiom-extension.toml"))]


def _permit(_envelope):
    return Verdict.from_decision(Decision.PERMIT, "test permits", f"t-{uuid.uuid4().hex}")


@pytest.fixture(scope="module")
def composed(tmp_path_factory):
    mp = pytest.MonkeyPatch()
    state = tmp_path_factory.mktemp("state")
    mp.setenv("AXIOM_STATE_DIR", str(state))
    mp.setenv("HOME", str(state))
    registry = RouterRegistry()
    hook = build_authz_hook(resolve_principal=build_bearer_resolver({}), decide_fn=_permit)
    app = compose_app(
        registry=registry,
        manifests=_manifests(),
        include_builtins=True,
        middleware=MiddlewareConfig(authz=hook),
        auto_authz=False,
    )
    specs = registry.specs()
    yield TestClient(app, raise_server_exceptions=False), specs
    mp.undo()


def _routes(specs):
    for spec in specs:
        for route in spec.router.routes:
            if isinstance(route, APIRoute):
                yield spec, route


def _owner(specs, path):
    best = None
    for spec in specs:
        if path == spec.prefix or path.startswith(spec.prefix.rstrip("/") + "/"):
            if best is None or len(spec.prefix) > len(best.prefix):
                best = spec
    return best


def _concrete(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "x", path)


def test_the_walk_sees_the_surfaces_that_matter(composed):
    _, specs = composed
    prefixes = {s.prefix for s in specs}
    # A walk that silently composed nothing would pass every assertion below.
    assert {"/api/v1", "/gate", "/v1", "/ingest"} <= prefixes, prefixes
    paths = {route.path for _, route in _routes(specs)}
    assert {"/api/v1/sites", "/api/v1/sites/{site}/channels"} <= paths


def test_every_anonymous_route_is_named_public(composed):
    _, specs = composed
    unnamed = []
    for _, route in _routes(specs):
        owner = _owner(specs, route.path)
        if requires_credential(owner, route.path):
            continue
        if route.path in PUBLIC_PATHS:
            continue
        if owner is not None and not owner.requires_authz and owner.prefix in PUBLIC_MOUNTS:
            continue
        unnamed.append(
            f"{sorted(route.methods)} {route.path} ({getattr(owner, 'extension', None)})"
        )
    assert not unnamed, (
        "routes answer without a credential and are not named public in this test:\n  "
        + "\n  ".join(unnamed)
        + "\nGate the mount (requires_authz=True) and declare only what must be public in "
        "MountSpec.public, then name it here with its reason."
    )


def test_a_public_path_on_a_gated_mount_is_exact(composed):
    """``/api/v1/health`` is public; ``/api/v1/health/sites`` lists every site and is not."""
    _, specs = composed
    owner = _owner(specs, "/api/v1/health/sites")
    assert owner is not None and owner.prefix == "/api/v1"
    assert requires_credential(owner, "/api/v1/health/sites")
    assert not requires_credential(owner, "/api/v1/health")


def test_every_gated_route_refuses_a_request_with_no_credential(composed):
    client, specs = composed
    answered = []
    for _, route in _routes(specs):
        owner = _owner(specs, route.path)
        if not requires_credential(owner, route.path):
            continue
        path = _concrete(route.path)
        for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
            response = client.request(method, path, json={} if method != "GET" else None)
            if response.status_code < 400 or response.status_code not in (401, 403):
                answered.append(f"{method} {path} -> {response.status_code}")
    assert not answered, "gated routes answered without a credential:\n  " + "\n  ".join(answered)
