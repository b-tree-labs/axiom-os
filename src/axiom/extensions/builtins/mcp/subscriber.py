# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Hook subscriber: refresh the MCP surface when the extension set changes.

Spec: ``docs/specs/spec-builtin-mcp-server.md`` §10.4.

Wired in the manifest as a hook on
``extension.post_install / .post_uninstall / .post_update``. The
subscriber regenerates the surface cache immediately (no debounce —
the install itself is the explicit user action).

Drift-detection (M-O heartbeat) is the *other* refresh path; see
``drift.py`` (Phase 1: stub) for that flow.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)


def on_extension_changed(event: dict[str, Any] | None = None) -> None:
    """Regenerate the cached MCP surface; called by the hook bus."""
    from axiom.extensions.builtins.mcp.aggregation import AggregationRegistry
    from axiom.extensions.builtins.mcp.cli import _write_cache

    try:
        surface = AggregationRegistry.from_node().build()
        cache_path = _write_cache(surface)
        log.info(
            "mcp.subscriber: regenerated surface (%d tools) -> %s",
            len(surface.tools),
            cache_path,
        )
    except Exception as exc:  # noqa: BLE001 — fail_mode = warn in manifest
        log.warning("mcp.subscriber: surface regen failed: %s", exc)


__all__ = ["on_extension_changed"]
