# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The sign-in page carries the node's product name.

The gate is the FIRST page an unauthenticated person sees — on a node whose
application surface is branded, a login card headed "Axiom" is the platform
introducing itself as the wrong product. `LoginBrand` could always express
this; nothing supplied it, so the default literal won on every deploy.
"""

import re

from fastapi import FastAPI
from fastapi.testclient import TestClient

from axiom.extensions.builtins.webgate.mount import mount_spec


def _login_html(monkeypatch, **env) -> str:
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    # The mount is built AFTER the environment is set: a spec assembled at
    # import time would carry whichever brand won the race.
    spec = mount_spec()
    app = FastAPI()
    app.include_router(spec.router)
    return TestClient(app).get("/gate/login").text


def test_the_login_page_is_headed_by_the_node_product(monkeypatch):
    html = _login_html(monkeypatch, AXIOM_BRAND_NAME="Acme OS")
    assert "<title>Sign in · Acme OS</title>" in html
    assert ">Acme OS<" in html


def test_the_platform_name_still_wins_when_nothing_is_set(monkeypatch):
    monkeypatch.delenv("AXIOM_BRAND_NAME", raising=False)
    html = _login_html(monkeypatch)
    assert "<title>Sign in · Axiom</title>" in html


def test_the_accent_follows_the_node_too(monkeypatch):
    html = _login_html(monkeypatch, AXIOM_BRAND_NAME="Acme OS", AXIOM_BRAND_ACCENT="#123456")
    assert "#123456" in html


def test_a_smuggled_accent_cannot_reach_the_stylesheet(monkeypatch):
    """`safe_accent()` already constrains this; the point is that routing the
    value through the resolver did not open a way around it."""
    html = _login_html(
        monkeypatch,
        AXIOM_BRAND_NAME="Acme OS",
        AXIOM_BRAND_ACCENT="red;} body{display:none",
    )
    assert "display:none" not in html
    assert "#bf5700" in html


def test_the_product_name_is_escaped(monkeypatch):
    html = _login_html(monkeypatch, AXIOM_BRAND_NAME="<script>alert(1)</script>")
    assert "<script>alert(1)</script>" not in html


def test_the_footer_still_names_the_platform(monkeypatch):
    """ADR-048: branding white-labels IDENTITY, not the borrowed platform
    underneath. The heading is who you are signing in to; the footer is what
    is protecting it, and that stays Axiom the way `systemd` keeps its name
    across distributions."""
    html = _login_html(monkeypatch, AXIOM_BRAND_NAME="Acme OS")
    assert "Protected by Axiom" in html


def test_the_gate_and_the_app_surface_agree(monkeypatch):
    """The invariant that makes the original defect impossible: two surfaces
    on ONE node cannot disagree about the product's name."""
    from axiom.extensions.builtins.receipts.mount import SurfaceBrand

    monkeypatch.setenv("AXIOM_BRAND_NAME", "Acme OS")
    html = _login_html(monkeypatch)
    app_name = SurfaceBrand().product_name
    assert app_name == "Acme OS"
    assert re.search(rf"<title>Sign in · {re.escape(app_name)}</title>", html)


def test_the_brand_reaches_the_built_spa_too(monkeypatch, tmp_path):
    """The gate has two renderers: the server-rendered card (what every node
    serves today, because `mount_spec` passes no `spa_dist`) and a built Vite
    bundle that reads `window.__AXIOM_GATE_BRAND__`.

    Both read the same `brand` argument, so supplying it at the mount fixed
    both at once. This asserts that rather than assuming it, because the SPA
    is the path a future deploy turns on and nobody would think to re-check.
    """
    from axiom.extensions.builtins.webgate.api.routers import (
        LoginBrand,
        build_webgate_router,
    )

    (tmp_path / "index.html").write_text(
        "<!doctype html><html><head></head><body></body></html>", encoding="utf-8"
    )
    monkeypatch.setenv("AXIOM_BRAND_NAME", "Acme OS")
    from axiom.infra.web_brand import resolve_web_brand

    resolved = resolve_web_brand()
    app = FastAPI()
    app.include_router(
        build_webgate_router(
            brand=LoginBrand(product_name=resolved.product_name, accent=resolved.accent),
            spa_dist=tmp_path,
        )
    )
    html = TestClient(app).get("/gate/login").text
    assert "__AXIOM_GATE_BRAND__" in html
    assert "Acme OS" in html
