# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""`kind = "api"` is not an unknown capability kind to publish (#1670).

An API surface is resolved by the web app from the manifest (`ApiSurfaceDef`),
not through a pyproject entry point, so, like `skill` and `recipe`, it has no
entry-point group to align. The publish scan and the validator both treated
it as an unknown kind and blocked the extension.
"""
from __future__ import annotations

from pathlib import Path

from axiom.cli.ext.commands import scan, validate

_API_BLOCK = {
    "kind": "api",
    "subpath": "/greeter",
    "entry": "greeter.api:register_routes",
    "description": "GET /api/v1/greeter/greet?name=Ada",
}


def _manifest() -> dict:
    return {"extension": {"name": "greeter", "version": "0.1.0", "provides": [_API_BLOCK]}}


def test_the_publish_scan_accepts_an_api_surface(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "greeter"\nversion = "0.1.0"\n', encoding="utf-8"
    )
    check = scan._check_manifest_pyproject_alignment(tmp_path, _manifest())
    assert check.severity == "pass", check.detail


def test_the_validator_accepts_an_api_surface():
    results = validate._check_entry_points(
        _manifest(), {"project": {"name": "greeter", "version": "0.1.0"}}
    )
    assert all(r.ok for r in results), [r.detail for r in results]


def test_a_truly_unknown_kind_is_still_refused(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "greeter"\nversion = "0.1.0"\n', encoding="utf-8"
    )
    manifest = {"extension": {"name": "greeter", "version": "0.1.0",
                              "provides": [{**_API_BLOCK, "kind": "nonsense"}]}}
    assert scan._check_manifest_pyproject_alignment(tmp_path, manifest).severity == "fail"
    assert not all(r.ok for r in validate._check_entry_points(manifest, {"project": {}}))
