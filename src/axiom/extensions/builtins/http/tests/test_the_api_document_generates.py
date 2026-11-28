# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The composed node can describe its own API.

``GET /openapi.json`` raised on a running node (2026-11-25, #2425): four routes
declared a Response class as their return type in a module with deferred
annotations and imported that class inside the function that registered them. FastAPI resolves a
return annotation against the handler's module, could not find the name, and
kept it as an unresolved reference; schema generation then failed for the whole
document. No test asked for the document, so it had been broken since the route
was added (0.70.33).

This composes every mount the platform ships, asks for the document, and names
any route whose response type did not resolve, so the failure points at the
route and not at pydantic.
"""

from __future__ import annotations

import tomllib
import uuid
from pathlib import Path

import pytest
from fastapi.routing import APIRoute

from axiom.extensions.builtins.http.authz_hook import build_authz_hook, build_bearer_resolver
from axiom.extensions.builtins.http.compose import compose_app
from axiom.extensions.builtins.http.middleware import MiddlewareConfig
from axiom.extensions.builtins.http.registry import RouterRegistry
from axiom.governance import Decision, Verdict

BUILTINS = Path(__file__).resolve().parents[2]


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
    hook = build_authz_hook(resolve_principal=build_bearer_resolver({}), decide_fn=_permit)
    registry = RouterRegistry()
    try:
        app = compose_app(
            registry=registry,
            manifests=_manifests(),
            include_builtins=True,
            middleware=MiddlewareConfig(authz=hook),
            auto_authz=False,
        )
        yield app, registry.specs()
    finally:
        mp.undo()


def _unresolved(specs) -> list[str]:
    """Documented routes whose response type is still a name, not a type.

    Read from each mount's own router: how the composed app holds an included
    router differs between FastAPI versions, the mount's routes do not."""
    found = set()
    for spec in specs:
        for route in spec.router.routes:
            if not isinstance(route, APIRoute) or not route.include_in_schema:
                continue
            if "ForwardRef" in repr(route.response_model):
                handler = route.endpoint
                found.add(f"{route.path} ({handler.__module__}.{handler.__qualname__})")
    return sorted(found)


def test_every_documented_route_has_a_response_type_that_resolves(composed):
    _app, specs = composed
    assert specs, "no mounts were composed, so this checked nothing"
    assert _unresolved(specs) == [], (
        "these handlers name a return type their module cannot resolve at run time; "
        "import the type at module level or pass response_model=None"
    )


def test_the_composed_node_generates_its_api_document(composed):
    app, _specs = composed
    document = app.openapi()
    assert document["paths"], "the document lists no paths"
    assert "/api/v1/site/sign-in" in document["paths"]
