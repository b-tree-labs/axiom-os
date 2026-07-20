# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""End-to-end coverage: memory + signals + hygiene through the MCP surface.

Spec: ``docs/specs/spec-builtin-mcp-server.md`` §12.6.

Each of the three extensions converts in Block B from the Phase-1
``_stub_handler`` to a real callable. These tests pin down the contract
the conversion has to honour:

- The extension's tools surface through ``AggregationRegistry.build()``
  with the spec-§6.5 default tool-name pattern (``axiom_<name>__<tool>``,
  with ``axiom_<name>_ext`` for the three extensions whose name collides
  with a platform module).
- ``dispatch_call`` on the surface returns a JSON-encodable result for
  each tool — no Phase-1 stub note, no NotImplementedError.
- The handlers honour the project rule that real on-disk fixtures (no
  database mocks) drive memory state.

The signals + hygiene status tools are read-only and self-bootstrapping,
so they need no fixture setup. The memory tool requires a tmp_axiom_home
to point CompositionService at a fresh on-disk SQLite ledger per test.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from axiom.extensions.builtins.mcp.aggregation import AggregationRegistry
from axiom.extensions.builtins.mcp.server import dispatch_call
from axiom.extensions.contracts import parse_manifest

REPO_ROOT = Path(__file__).resolve().parents[7]
BUILTINS = REPO_ROOT / "src" / "axiom" / "extensions" / "builtins"


pytestmark = pytest.mark.skip(
    reason=(
        "Depends on the 29 axiom-extension.toml manifests having [extension.mcp] "
        "blocks declared, and on the hygiene/signals mcp_handlers shipped in "
        "Branch D (feat/hygiene-signals-mcp-handlers). Will re-enable when those "
        "follow-on extractions land."
    )
)


def _load_builtin(name: str):
    """Parse a built-in extension's manifest into an ``Extension``."""
    manifest = BUILTINS / name / "axiom-extension.toml"
    assert manifest.exists(), f"missing {manifest}"
    return parse_manifest(manifest)


@pytest.fixture
def three_ext_surface(tmp_axiom_home):
    """Build a surface containing memory + signals + hygiene contributions."""
    exts = [_load_builtin(n) for n in ("memory", "signals", "hygiene")]
    registry = AggregationRegistry(extensions=exts)
    return registry.build()


# ---------------------------------------------------------------------------
# Surface composition
# ---------------------------------------------------------------------------


def test_memory_extension_surface_present(three_ext_surface):
    names = [t.name for t in three_ext_surface.tools]
    # Per spec §6.5, an extension named "memory" defaults to the
    # `axiom_memory_ext` prefix to avoid colliding with platform tools.
    assert "axiom_memory_ext__show" in names, (
        f"expected axiom_memory_ext__show, got {names}"
    )


def test_signals_extension_surface_present(three_ext_surface):
    names = [t.name for t in three_ext_surface.tools]
    assert "axiom_signals_ext__status" in names, (
        f"expected axiom_signals_ext__status, got {names}"
    )


def test_hygiene_extension_surface_present(three_ext_surface):
    names = [t.name for t in three_ext_surface.tools]
    assert "axiom_hygiene_ext__status" in names, (
        f"expected axiom_hygiene_ext__status, got {names}"
    )


def test_all_three_extensions_have_contributions(three_ext_surface):
    """Provenance lists all three extensions as contributors."""
    names = {s.name for s in three_ext_surface.sources if s.kind == "extension"}
    assert {"memory", "signals", "hygiene"} <= names


# ---------------------------------------------------------------------------
# Real dispatch — JSON-encodable results, no stub notes
# ---------------------------------------------------------------------------


def test_memory_show_returns_real_payload(three_ext_surface):
    out = asyncio.run(
        dispatch_call(
            three_ext_surface,
            "axiom_memory_ext__show",
            {"principal": "@bench:test", "limit": 5},
        )
    )
    assert len(out) == 1
    payload = json.loads(out[0].text)
    assert "Phase 1 stub" not in str(payload), payload
    assert "fragment_count" in payload, payload
    assert payload["principal"] == "@bench:test"


def test_signals_status_returns_real_payload(three_ext_surface):
    out = asyncio.run(
        dispatch_call(three_ext_surface, "axiom_signals_ext__status", {})
    )
    payload = json.loads(out[0].text)
    assert "Phase 1 stub" not in str(payload), payload
    # Signals status returns a dict whose keys describe inbox / processed
    # / drafts. The exact set may evolve; we only require dict-shape and
    # the presence of one well-known key.
    assert isinstance(payload, dict), payload
    assert "inbox" in payload or "processed" in payload or "ok" in payload, payload


def test_hygiene_status_returns_real_payload(three_ext_surface):
    out = asyncio.run(
        dispatch_call(three_ext_surface, "axiom_hygiene_ext__status", {})
    )
    payload = json.loads(out[0].text)
    assert "Phase 1 stub" not in str(payload), payload
    assert isinstance(payload, dict), payload
    # M-O status returns at least the base_dir and disk metrics.
    assert "base_dir" in payload or "ok" in payload, payload


# ---------------------------------------------------------------------------
# Negative paths
# ---------------------------------------------------------------------------


def test_unknown_extension_tool_returns_structured_error(three_ext_surface):
    out = asyncio.run(
        dispatch_call(
            three_ext_surface, "axiom_memory_ext__does_not_exist", {}
        )
    )
    payload = json.loads(out[0].text)
    assert "error" in payload, payload
