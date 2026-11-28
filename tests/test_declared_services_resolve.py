# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0
"""A declared service must exist (#1267, #1268).

A manifest that says it provides a service the platform cannot load fails
silently: nothing starts, nothing can be proven never-down. Every built-in
`kind = "service"` entry must resolve to a real object, or say `optional = true`.
"""

from __future__ import annotations

import importlib
import tomllib
from pathlib import Path

import pytest

BUILTINS = Path(__file__).resolve().parents[1] / "src/axiom/extensions/builtins"


def _declared_services():
    for manifest in sorted(BUILTINS.glob("*/axiom-extension.toml")):
        data = tomllib.loads(manifest.read_text())
        for p in data.get("extension", {}).get("provides", []):
            if p.get("kind") == "service":
                yield manifest.parent.name, p


SERVICES = list(_declared_services())


def test_services_were_found():
    assert SERVICES, "manifest scan found no services; the guard would pass vacuously"


@pytest.mark.parametrize("ext,svc", SERVICES, ids=lambda v: v if isinstance(v, str) else v["name"])
def test_declared_service_resolves(ext, svc):
    if svc.get("optional") is True:
        pytest.skip("declared optional")
    module, _, attr = svc["entry"].partition(":")
    try:
        mod = importlib.import_module(module)
    except ImportError as e:
        pytest.fail(f"{ext}: service {svc['name']!r} entry {svc['entry']!r} does not import: {e}")
    assert attr and hasattr(mod, attr), (
        f"{ext}: service {svc['name']!r} entry {svc['entry']!r}: no attribute {attr!r}"
    )


def test_no_placeholder_service_in_observability():
    data = tomllib.loads((BUILTINS / "observability/axiom-extension.toml").read_text())
    names = [p["name"] for p in data["extension"]["provides"] if p.get("kind") == "service"]
    assert names == [], "observability declared a service that is a heartbeat stub (#1268)"
