# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The composed node's front door: "/" is never a JSON not_found.

Found live (2026-09-24): an OIDC round-trip whose ``next`` defaulted to
"/" landed on the substrate's unrouted root — the error middleware's
JSON 404 — because every mount lives under a prefix.
"""

from __future__ import annotations

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from axiom.extensions.builtins.http.compose import compose_app  # noqa: E402
from axiom.extensions.builtins.http.registry import MountSpec, RouterRegistry  # noqa: E402


def _registry() -> RouterRegistry:
    reg = RouterRegistry()
    router = fastapi.APIRouter()

    @router.get("/demo/")
    async def demo() -> dict:
        return {"ok": True}

    reg.register(
        MountSpec(
            prefix="/demo",
            router=router,
            extension="demo",
            requires_authz=False,
            profiles=("server",),
        )
    )
    return reg


def _client(monkeypatch, surface: str | None) -> TestClient:
    if surface is None:
        monkeypatch.delenv("AXIOM_DEFAULT_SURFACE", raising=False)
    else:
        monkeypatch.setenv("AXIOM_DEFAULT_SURFACE", surface)
    app = compose_app(
        profile="server",
        registry=_registry(),
        include_builtins=False,
        auto_authz=False,
        bind_host="127.0.0.1",
    )
    return TestClient(app, follow_redirects=False)


def test_default_surface_routes_the_root(monkeypatch):
    c = _client(monkeypatch, "/demo/")
    r = c.get("/")
    assert r.status_code == 302
    assert r.headers["location"] == "/demo/"


def test_without_config_the_root_is_an_honest_index(monkeypatch):
    c = _client(monkeypatch, None)
    r = c.get("/")
    assert r.status_code == 200
    assert "Axiom node" in r.text
    assert "/demo" in r.text  # names the mounted surface


def test_a_non_local_target_falls_back_to_the_index(monkeypatch):
    c = _client(monkeypatch, "https://evil.example/")
    r = c.get("/")
    assert r.status_code == 200  # index, never an open redirect
    assert "Axiom node" in r.text
