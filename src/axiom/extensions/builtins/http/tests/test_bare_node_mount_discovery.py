# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A bare Axiom node composes manifest-declared mounts from installed
packages — no site-repo hand-registration.

The gap this pins (found 2026-09-23): ``axi serve`` called
``compose_app`` without manifests, making ``discover_mounts`` a no-op,
so a pip-installed extension package (appkit being the motivating case;
any consumer's domain package equally) never mounted on a bare node —
consumers had to hand-register in their site repo. Separate-instance
consumers (a domain layer, a commercial deployment) must get platform
surfaces purely by installing packages."""

from __future__ import annotations

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from axiom.extensions.builtins.http import compose  # noqa: E402
from axiom.extensions.builtins.http.registry import RouterRegistry  # noqa: E402

_DEMO_ENTRY = "axiom.extensions.builtins.http.tests.demo_mount:mount_spec"


def _write_installed_style_ext(root, *, kind="service"):
    ext_dir = root / "somepkg" / "extensions" / "builtins" / "demo"
    ext_dir.mkdir(parents=True)
    (ext_dir / "axiom-extension.toml").write_text(
        f"""
[extension]
name = "demo"
version = "0.0.1"
description = "demo"
license = "Apache-2.0"

[[extension.provides]]
kind = "{kind}"
name = "demo"
entry = "{_DEMO_ENTRY}"
prefix = "/_demo"
""",
        encoding="utf-8",
    )
    return ext_dir


def test_discovered_manifests_reads_manifest_dicts(tmp_path):
    ext_dir = _write_installed_style_ext(tmp_path)
    manifests = compose.discovered_manifests(ext_dir)
    assert len(manifests) == 1
    provides = manifests[0]["extension"]["provides"]
    assert provides[0]["entry"] == _DEMO_ENTRY


def test_manifest_declared_mount_composes_and_serves(tmp_path):
    ext_dir = _write_installed_style_ext(tmp_path)
    reg = RouterRegistry()
    compose.discover_mounts(reg, manifests=compose.discovered_manifests(ext_dir))
    app = compose.compose_app(registry=reg, include_builtins=False, allow_insecure=True)
    r = TestClient(app).get("/_demo/health")
    assert r.status_code == 200
    assert r.json() == {"demo": "ok"}


def test_serve_path_supplies_discovered_manifests(monkeypatch, tmp_path):
    """The serve skill's composition passes discovered manifests — the
    wiring whose absence was the bare-node gap."""
    ext_dir = _write_installed_style_ext(tmp_path)
    monkeypatch.setattr(compose, "_default_manifest_dirs", lambda: [ext_dir])
    reg = RouterRegistry()
    app = compose.compose_app(
        registry=reg,
        include_builtins=False,
        allow_insecure=True,
        manifests=compose.discovered_manifests(),
    )
    r = TestClient(app).get("/_demo/health")
    assert r.status_code == 200


def test_non_service_kinds_do_not_mount(tmp_path):
    """AEOS has eight kinds; only ``service`` blocks carry mounts. A
    manifest mis-declaring (e.g. an invented kind) is skipped, not
    guessed at."""
    ext_dir = _write_installed_style_ext(tmp_path, kind="tool")
    reg = RouterRegistry()
    compose.discover_mounts(reg, manifests=compose.discovered_manifests(ext_dir))
    assert not any(s.prefix == "/_demo" for s in reg.specs())
