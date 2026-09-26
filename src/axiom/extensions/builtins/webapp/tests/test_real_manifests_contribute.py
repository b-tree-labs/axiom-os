# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Every REAL builtin manifest's ``kind = "api"`` block must collect.

The class this guards (found live, 2026-09-24): a builtin shipped an
api block with no ``subpath`` — the collector's ValueError then took
down EVERY /api/v1 contribution on a composed node (fleet, chat, all
of them 404), while unit tests that compose routers directly stayed
green. Discovery of the real manifests IS the test subject here.
"""

from __future__ import annotations

from axiom.extensions.builtins.webapp.api.contributions import collect_contributions
from axiom.extensions.discovery import discover_extensions


def test_every_builtin_api_block_collects():
    extensions = list(discover_extensions())
    assert extensions, "discovery found no extensions — the test would be vacuous"
    contributions = collect_contributions(extensions)  # raises on any bad block
    by_path = {c.subpath: c.extension for c in contributions}
    # The known contributors stay present (regression floor, not a ceiling).
    assert "/fleet" in by_path
    assert "/chat" in by_path
    assert "/receipts" in by_path


def test_collected_entries_resolve_and_register():
    """Each contributed entry must import and register its routes — a
    typo'd entry is a 404 surface, not a deploy-time error, unless this
    asserts it."""
    import fastapi

    from axiom.extensions.builtins.webapp.api.contributions import apply_contributions

    router = fastapi.APIRouter(prefix="/api/v1")
    contributions = collect_contributions(list(discover_extensions()))
    landed = apply_contributions(router, contributions)
    assert set(landed) >= {"/fleet", "/chat", "/receipts"}
    paths = {r.path for r in router.routes}
    assert any(p.startswith("/api/v1/receipts") for p in paths)
    assert any(p.startswith("/api/v1/fleet") for p in paths)
