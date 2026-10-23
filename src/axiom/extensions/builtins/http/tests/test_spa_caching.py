# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A returning browser must not be left holding last week's page.

The failure this pins down looks like "the app has no styling applied",
and it is a caching bug wearing a design bug's clothes. A Vite bundle
splits into an entry document that NAMES content-hashed assets and the
assets themselves. Those two have opposite caching needs, and getting
them backwards is what produces the symptom:

- the entry document must be revalidated every load, because it is the
  only thing that knows which hashes are current. Served with no
  directives at all, a browser is free to reuse it heuristically — and
  then it asks for a stylesheet hash that a rebuild has moved on from.
- the assets must cache forever, because the hash IS the version. A new
  build is a new URL, so there is nothing to invalidate.

``spa.py`` already promised the second half in prose ("assets are
content-hashed and cache hard") while shipping neither half — a claim
in a docstring that the code does not keep. These tests are what make
the claim true.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from axiom.extensions.builtins.http.spa import build_spa_router


def _built(tmp_path: Path, *, css: str = "index-AAA111.css") -> Path:
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True, exist_ok=True)
    (dist / "assets" / css).write_text(".axk-shell{color:red}", encoding="utf-8")
    (dist / "index.html").write_text(
        '<!doctype html><html><head><link rel="stylesheet" href="/s/assets/'
        f'{css}"></head><body><div id="root"></div>'
        '<script type="module" src="/s/assets/index.js"></script></body></html>',
        encoding="utf-8",
    )
    return dist


@pytest.fixture()
def client(tmp_path):
    app = FastAPI()
    app.include_router(
        build_spa_router(
            prefix="/s",
            dist=_built(tmp_path),
            bootstrap_global="__BRAND__",
            bootstrap_payload={"product_name": "Axiom"},
            placeholder_html="<p>not built</p>",
        )
    )
    return TestClient(app), tmp_path


def test_the_entry_document_is_revalidated_every_load(client):
    """The page that names the hashes may never be served from cache
    without asking — that is the whole defence against a stale bundle."""
    c, _ = client
    r = c.get("/s/")
    assert r.status_code == 200
    cache = r.headers.get("cache-control", "")
    assert "no-cache" in cache, f"entry document must revalidate, got {cache!r}"
    # ...and it must not be marked cacheable for any span of time, which
    # would let a browser skip the revalidation it was just told to do.
    assert "max-age=0" in cache or "no-store" in cache or "must-revalidate" in cache


def test_content_hashed_assets_cache_hard(client):
    """The hash is the version, so the URL is immutable. Revalidating it
    on every navigation is pure latency for an answer that cannot change."""
    c, _ = client
    r = c.get("/s/assets/index-AAA111.css")
    assert r.status_code == 200
    cache = r.headers.get("cache-control", "")
    assert "immutable" in cache, f"hashed asset should be immutable, got {cache!r}"
    assert "max-age=31536000" in cache


def test_a_rebuild_is_visible_on_the_very_next_request(client):
    """The index is read per request, so a rebuild needs no restart. This
    is the property the no-cache header exists to carry to the browser."""
    c, tmp_path = client
    assert "index-AAA111.css" in c.get("/s/").text
    _built(tmp_path, css="index-BBB222.css")  # a CSS-only rebuild: new hash
    body = c.get("/s/").text
    assert "index-BBB222.css" in body
    assert "index-AAA111.css" not in body


def test_the_unbuilt_placeholder_is_never_cached(client, tmp_path):
    """A surface that is merely not built yet must not stick in a
    browser after the build lands."""
    app = FastAPI()
    app.include_router(
        build_spa_router(
            prefix="/s",
            dist=tmp_path / "absent",
            bootstrap_global="__BRAND__",
            bootstrap_payload={},
            placeholder_html="<p>not built</p>",
        )
    )
    r = TestClient(app).get("/s/")
    assert r.status_code == 200 and "not built" in r.text
    assert "no-cache" in r.headers.get("cache-control", "")
