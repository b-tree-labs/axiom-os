# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Foundation tests for the ``webapp`` /api/v1 mount."""

from __future__ import annotations

from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from axiom.extensions.builtins.http.registry import MountSpec
from axiom.extensions.builtins.webapp import __version__
from axiom.extensions.builtins.webapp.mount import API_PREFIX, mount_spec


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(mount_spec().router)
    return TestClient(app)


def test_mount_spec_shape() -> None:
    spec = mount_spec()
    assert isinstance(spec, MountSpec)
    assert spec.prefix == API_PREFIX == "/api/v1"
    assert spec.extension == "webapp"
    assert spec.profiles == ("server",)
    assert isinstance(spec.router, APIRouter)
    # Per-route auth (webauth deps) guards mutating routes, not the mount gate.
    assert spec.requires_authz is False


def test_health_ok() -> None:
    resp = _client().get("/api/v1/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "service": "webapp"}


def test_version_reports_api_v1() -> None:
    resp = _client().get("/api/v1/version")
    assert resp.status_code == 200
    body = resp.json()
    assert body["api"] == "v1"
    assert body["service"] == "webapp"
    assert body["version"] == __version__


def test_routes_land_under_api_v1_namespace() -> None:
    """Nothing escapes the namespace, whatever is installed.

    This asserted an exact route set, which was right while webapp owned every
    route. Verbs now contribute their own under /api/v1, so the set varies with
    what is installed and an exact assertion would fail on a correct install and
    pass on an empty one — the opposite of useful.

    The invariant is what mattered all along: every route is inside the version
    namespace, and webapp's own routes are present.
    """
    paths = {route.path for route in mount_spec().router.routes}

    assert {"/api/v1/health", "/api/v1/version"} <= paths, (
        "webapp's own routes went missing"
    )
    escaped = sorted(p for p in paths if not p.startswith(API_PREFIX + "/"))
    assert not escaped, f"routes escaped the {API_PREFIX} namespace: {escaped}"
