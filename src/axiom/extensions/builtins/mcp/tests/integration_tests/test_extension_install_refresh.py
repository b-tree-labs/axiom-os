# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Integration test: ``extension.post_install`` refreshes the surface cache.

Spec: ``docs/specs/spec-builtin-mcp-server.md`` §10.4 + §12.6.

The MCP built-in subscribes to ``extension.post_install`` /
``.post_uninstall`` / ``.post_update`` via the manifest hook block. When
those events fire the subscriber regenerates the surface cache
immediately — no debounce because the install is the explicit user
action.

This test fires ``on_extension_changed`` directly (the entry point the
hook bus would call) and verifies the cache changes in a way only a real
re-walk can produce: the content hash before the call differs from the
content hash after the call when the manifest set has changed in
between.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from axiom.extensions.builtins.mcp.subscriber import on_extension_changed

pytestmark = pytest.mark.skip(
    reason=(
        "Depends on the 29 axiom-extension.toml manifests having [extension.mcp] "
        "blocks declared, and on the hygiene/signals mcp_handlers shipped in "
        "Branch D (feat/hygiene-signals-mcp-handlers). Will re-enable when those "
        "follow-on extractions land."
    )
)


def _read_cache_hash(home: Path) -> str | None:
    cache = home / "mcp" / "surface.json"
    if not cache.exists():
        return None
    return json.loads(cache.read_text(encoding="utf-8")).get("content_hash")


def test_post_install_subscriber_writes_cache(tmp_axiom_home):
    """First-call: writes the cache where there was none."""
    cache = tmp_axiom_home / "mcp" / "surface.json"
    assert not cache.exists()

    on_extension_changed({"event": "extension.post_install"})

    assert cache.exists(), "subscriber should have created the surface cache"
    payload = json.loads(cache.read_text(encoding="utf-8"))
    assert "content_hash" in payload
    assert "tools" in payload


def test_post_install_subscriber_idempotent_when_clean(tmp_axiom_home):
    """Two consecutive fires against the same extension set produce the same hash."""
    on_extension_changed({"event": "extension.post_install"})
    h1 = _read_cache_hash(tmp_axiom_home)
    on_extension_changed({"event": "extension.post_update"})
    h2 = _read_cache_hash(tmp_axiom_home)
    assert h1 == h2, "idempotent regen must not change the content hash"


def test_post_install_subscriber_updates_cache_on_change(
    tmp_axiom_home, monkeypatch
):
    """When discovery returns a different extension set the hash flips."""
    # First fire — captures the live discovery hash for tmp_axiom_home.
    on_extension_changed({"event": "extension.post_install"})
    h1 = _read_cache_hash(tmp_axiom_home)
    assert h1, "expected an initial cache hash"

    # Now monkey-patch discovery to drop *all* extensions. The fresh
    # rebuild will only contain platform primitives → different hash.
    from axiom.extensions.builtins.mcp import aggregation as agg_mod

    monkeypatch.setattr(
        agg_mod, "_build_extension_tool_surface",
        lambda *a, **kw: ([], {}, []),
    )

    # The aggregation registry's `from_node` walks discovery; a simpler
    # injection: monkey-patch discover_extensions to return [].
    from axiom.extensions import discovery as disc_mod

    monkeypatch.setattr(disc_mod, "discover_extensions", lambda: iter([]))

    on_extension_changed({"event": "extension.post_uninstall"})
    h2 = _read_cache_hash(tmp_axiom_home)
    assert h2, "expected a refreshed cache hash"
    assert h2 != h1, (
        "discovery returned a different extension set; the post-install "
        "subscriber must refresh the cache to a new content hash"
    )


def test_post_install_refresh_clears_drift(tmp_axiom_home):
    """After a post-install refresh, drift detection returns None."""
    from axiom.extensions.builtins.mcp.drift import check_mcp_surface_drift

    on_extension_changed({"event": "extension.post_install"})
    assert check_mcp_surface_drift(node_root=tmp_axiom_home) is None
