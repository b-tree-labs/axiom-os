# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The ``/ingest`` mount is one namespace claim carrying both lanes
(ADR-106; spec-signal-ingest-and-producer §5.1)."""

from __future__ import annotations

import pytest


def test_ingest_mount_carries_both_lanes():
    pytest.importorskip("fastapi")
    from axiom.extensions.builtins.http.mounts import ingest_mount_spec

    spec = ingest_mount_spec()
    assert spec.prefix == "/ingest"
    assert spec.extension == "data_platform"
    assert spec.requires_authz is True
    # the route table the middleware builds reads spec.router.routes directly —
    # both lanes must be concrete routes there, not a lazy included router
    direct = {
        (r.path, tuple(sorted(r.methods or ()))) for r in spec.router.routes if hasattr(r, "path")
    }
    assert ("/ingest", ("POST",)) in direct
    assert ("/ingest/rows", ("POST",)) in direct
    assert all(hasattr(r, "path") for r in spec.router.routes)

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    # FastAPI materialises included routers lazily, so probe the lanes as a
    # client would: an empty body is a validation error (422) on a lane that
    # exists and a 404 on one that does not.
    app = FastAPI()
    app.include_router(spec.router)
    client = TestClient(app)
    assert client.post("/ingest", json={}).status_code == 422
    assert client.post("/ingest/rows", json={}).status_code == 422
    assert client.post("/ingest/nope", json={}).status_code == 404


def test_ingest_face_reaches_the_composed_app_through_compose_app(monkeypatch):
    """End to end through compose_app + middleware (the path the node runs):
    a legacy token passes the hook and an empty body is a 422 on both lanes."""
    pytest.importorskip("fastapi")
    monkeypatch.setenv("AXIOM_HTTP_API_KEYS", "tok-1:@svc:local")
    monkeypatch.setenv("AXIOM_MODE", "dev")
    from fastapi.testclient import TestClient

    from axiom.extensions.builtins.http.compose import compose_app
    from axiom.extensions.builtins.http.mounts import ingest_mount_spec
    from axiom.extensions.builtins.http.registry import RouterRegistry

    reg = RouterRegistry()
    reg.register(ingest_mount_spec())
    app = compose_app(registry=reg, include_builtins=False, bind_host="127.0.0.1")
    c = TestClient(app)
    h = {"Authorization": "Bearer tok-1"}
    assert c.post("/ingest/rows", json={}, headers=h).status_code == 422
    assert c.post("/ingest", json={}, headers=h).status_code == 422
