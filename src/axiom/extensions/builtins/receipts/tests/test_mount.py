# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The /receipts/ serving skeleton (ADR-123 D2, spec-receipts-surface §3).

The surface is an appkit-shell app served the webgate way: an optional
built Vite bundle, brand injected as a bootstrap global, assets guarded
against traversal, gate-fronted (requires_authz). Without a build the
mount serves an HONEST placeholder that names the build step — never a
staged UI."""

from __future__ import annotations

from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from axiom.extensions.builtins.receipts import mount as receipts_mount  # noqa: E402

_WEBUI = Path(receipts_mount.__file__).parent / "webui"


def _client(spa_dist=None):
    app = fastapi.FastAPI()
    app.include_router(receipts_mount.build_receipts_router(spa_dist=spa_dist))
    return TestClient(app)


def _dist(tmp_path: Path) -> Path:
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text(
        '<html><head></head><body><script type="module" src="/receipts/assets/app.js">'
        "</script></body></html>",
        encoding="utf-8",
    )
    (dist / "assets" / "app.js").write_text("console.log('receipts')", encoding="utf-8")
    return dist


def test_mount_spec_is_gate_fronted_under_receipts():
    spec = receipts_mount.mount_spec()
    assert spec.prefix == "/receipts"
    assert spec.requires_authz is True
    assert spec.extension == "receipts"


def test_without_a_build_the_placeholder_is_honest(tmp_path):
    # An explicitly empty dist, NOT the default path: the default is the
    # in-tree webui/dist, which exists on any machine that has run the
    # build — this test's first sweep failure was exactly that
    # environment dependence.
    r = _client(spa_dist=tmp_path / "no-dist").get("/receipts/")
    assert r.status_code == 200
    body = r.text
    assert "not built" in body.lower()
    assert "npm" in body  # names the build step instead of faking a surface


def test_default_dist_is_the_in_tree_webui_build():
    assert Path(receipts_mount.__file__).parent / "webui" / "dist" == receipts_mount._DEFAULT_DIST


def test_index_gets_the_brand_bootstrap_before_the_bundle(tmp_path):
    r = _client(_dist(tmp_path)).get("/receipts/")
    assert r.status_code == 200
    body = r.text
    assert "window.__AXIOM_BRAND__" in body
    # the global must be set before the module script boots
    assert body.index("window.__AXIOM_BRAND__") < body.index('<script type="module"')


def test_assets_serve_and_traversal_is_refused(tmp_path):
    client = _client(_dist(tmp_path))
    ok = client.get("/receipts/assets/app.js")
    assert ok.status_code == 200 and "receipts" in ok.text
    for sneaky in ("../index.html", "..%2Findex.html", "a/../../secret"):
        assert client.get(f"/receipts/assets/{sneaky}").status_code == 404


def test_webui_scaffold_pins_the_vite_base():
    """The bundle must build against base '/receipts/' (spec §3) and link the
    appkit token stylesheet — the one-UI-infra contract, checkable at rest."""
    vite = (_WEBUI / "vite.config.ts").read_text(encoding="utf-8")
    assert "base: '/receipts/'" in vite or 'base: "/receipts/"' in vite
    index = (_WEBUI / "index.html").read_text(encoding="utf-8")
    assert "/_appkit/tokens.css" in index


def test_the_committed_bundle_is_internally_consistent():
    """The build output is tracked in git, so a branch switch can leave
    one build's index.html beside another build's assets. Everything
    still answers 200; the browser just gets an app whose stylesheet
    does not exist. This is the check that notices."""
    from axiom.extensions.builtins.http.spa import audit_bundle
    from axiom.extensions.builtins.receipts.mount import WEBUI_DIR

    dist = WEBUI_DIR / "dist"
    if not (dist / "index.html").is_file():
        pytest.skip("no build present — the mount serves its honest placeholder")
    audit = audit_bundle(dist)
    assert audit.consistent, (
        f"the committed receipts bundle does not line up — {audit.why()}. "
        "Rebuild it (npm run build in webui/) and commit index.html WITH its assets."
    )
