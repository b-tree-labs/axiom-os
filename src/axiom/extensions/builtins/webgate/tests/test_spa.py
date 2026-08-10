# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The SPA-serving path of the webgate router.

When ``build_webgate_router`` is handed a built Vite bundle (``spa_dist``), it
serves that single brand-neutral bundle with the per-consumer brand injected at
``window.__AXIOM_GATE_BRAND__``. When ``spa_dist`` is absent it falls back to the
server-rendered login page (covered by ``test_gate.py``). These tests drive the
SPA path against a tiny fake dist so they never depend on a real ``npm run build``.
"""

from __future__ import annotations

import warnings
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from axiom.extensions.builtins.webgate.api.routers import LoginBrand, build_webgate_router
from axiom.webauth import get_password_hash
from axiom.webauth.keys import reset_key_store_for_tests
from axiom.webauth.users import InMemoryUserStore, User

warnings.filterwarnings("ignore")

PW = "correct-horse-battery-staple"


@pytest.fixture(autouse=True)
def _keys():
    reset_key_store_for_tests()
    yield
    reset_key_store_for_tests()


def _store():
    return InMemoryUserStore([
        User(user_id="u1", email="alice@example.org",
             password_hash=get_password_hash(PW), name="Alice", roles=("user",)),
    ])


@pytest.fixture
def fake_dist(tmp_path: Path) -> Path:
    """A minimal built bundle: an index.html with a <head> + module script, one
    asset, and a favicon — the shape the real Vite build emits."""
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text(
        "<!doctype html><html lang=\"en\"><head>"
        '<meta charset="utf-8">'
        '<script type="module" crossorigin src="/gate/assets/app.js"></script>'
        "</head><body><div id=\"root\"></div></body></html>",
        encoding="utf-8",
    )
    (dist / "assets" / "app.js").write_text("console.log('gate app');", encoding="utf-8")
    (dist / "favicon.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16"></svg>', encoding="utf-8"
    )
    return dist


def _spa_client(dist: Path, brand: LoginBrand | None = None) -> TestClient:
    app = FastAPI()
    app.include_router(
        build_webgate_router(_store(), secure_cookies=False, brand=brand, spa_dist=dist)
    )
    return TestClient(app, base_url="http://gate.example", follow_redirects=False)


def test_spa_login_serves_built_index_with_injected_brand(fake_dist):
    c = _spa_client(fake_dist, brand=LoginBrand(product_name="TRIGA Chat"))
    r = c.get("/gate/login")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    # The runtime-brand global is present and carries the passed product name.
    assert "window.__AXIOM_GATE_BRAND__" in r.text
    assert '"productName": "TRIGA Chat"' in r.text
    # It is injected BEFORE the module script so the global is set before boot.
    assert r.text.index("__AXIOM_GATE_BRAND__") < r.text.index('type="module"')


def test_spa_forgot_serves_the_same_bundle(fake_dist):
    r = _spa_client(fake_dist).get("/gate/forgot")
    assert r.status_code == 200
    assert "window.__AXIOM_GATE_BRAND__" in r.text
    # Default brand → Axiom product name.
    assert '"productName": "Axiom"' in r.text


def test_spa_assets_are_served(fake_dist):
    r = _spa_client(fake_dist).get("/gate/assets/app.js")
    assert r.status_code == 200
    assert "console.log" in r.text


def test_spa_favicon_is_served(fake_dist):
    r = _spa_client(fake_dist).get("/gate/favicon.svg")
    assert r.status_code == 200
    assert "image/svg+xml" in r.headers["content-type"]


def test_spa_asset_path_traversal_is_refused(fake_dist):
    # A sibling secret outside assets/ must never be reachable through the route.
    (fake_dist.parent / "secret.txt").write_text("TOPSECRET", encoding="utf-8")
    r = _spa_client(fake_dist).get("/gate/assets/../../secret.txt")
    assert r.status_code != 200
    assert "TOPSECRET" not in r.text


def test_spa_missing_asset_is_404(fake_dist):
    r = _spa_client(fake_dist).get("/gate/assets/nope.js")
    assert r.status_code == 404


def test_brand_logo_svg_cannot_break_out_of_the_script(fake_dist):
    # A </script> smuggled into the logo SVG must be neutralized in the JSON.
    evil = "<svg></svg></script><script>alert(1)</script>"
    r = _spa_client(fake_dist, brand=LoginBrand(product_name="X", logo=evil)).get("/gate/login")
    assert r.status_code == 200
    assert "</script><script>alert(1)" not in r.text
    # The escaped form (<) is what actually lands in the document.
    assert "\\u003c/script>" in r.text


def test_session_login_still_works_under_spa_mode(fake_dist):
    # The JSON auth seam the SPA calls must behave identically with spa_dist set.
    c = _spa_client(fake_dist)
    r = c.post("/gate/session", json={"email": "alice@example.org", "password": PW})
    assert r.status_code == 200
    assert r.json()["email"] == "alice@example.org"
    assert c.get("/gate/verify").status_code == 200


def test_spa_dist_none_falls_back_to_server_rendered_page():
    # No spa_dist → the current server-rendered password form, unchanged.
    app = FastAPI()
    app.include_router(build_webgate_router(_store(), secure_cookies=False))
    r = TestClient(app, base_url="http://gate.example").get("/gate/login")
    assert r.status_code == 200
    assert 'name="password"' in r.text
    assert "window.__AXIOM_GATE_BRAND__" not in r.text


def test_spa_dist_pointing_at_dir_without_index_falls_back(tmp_path):
    # A dist path that has no index.html is treated as "no build" → server-rendered.
    empty = tmp_path / "empty"
    empty.mkdir()
    app = FastAPI()
    app.include_router(build_webgate_router(_store(), secure_cookies=False, spa_dist=empty))
    r = TestClient(app, base_url="http://gate.example").get("/gate/login")
    assert r.status_code == 200
    assert 'name="password"' in r.text
