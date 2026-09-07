# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Test config for the built-in MCP root-server extension.

``axiom-tests`` is a ``pytest11`` plugin — installing it in the active
environment is sufficient for its fixtures (``tmp_axiom_home`` etc.) to
be available here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from axiom.extensions.contracts import Extension

# ---------------------------------------------------------------------------
# Fixture manifests directory
# ---------------------------------------------------------------------------

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "manifests"


@pytest.fixture
def fixture_manifest_path():
    """Return a callable mapping fixture name -> Path."""

    def _resolve(name: str) -> Path:
        path = FIXTURES_DIR / name
        if not path.exists():
            raise FileNotFoundError(
                f"Fixture manifest {name} not found under {FIXTURES_DIR}"
            )
        return path

    return _resolve


# ---------------------------------------------------------------------------
# Synthetic extension factory
# ---------------------------------------------------------------------------


@pytest.fixture
def make_extension(tmp_path: Path):
    """Build an ``Extension`` object backed by a tmp manifest on disk.

    Tests can inject the exact ``[extension.mcp]`` block(s) they need
    without coupling to the discovery walker.
    """

    counter = {"n": 0}

    def _make(name: str, manifest_body: str, *, builtin: bool = False) -> Extension:
        counter["n"] += 1
        ext_root = tmp_path / f"ext_{counter['n']}_{name}"
        ext_root.mkdir(parents=True, exist_ok=True)
        manifest_path = ext_root / "axiom-extension.toml"
        manifest_path.write_text(manifest_body, encoding="utf-8")
        from axiom.extensions.contracts import parse_manifest

        ext = parse_manifest(manifest_path)
        ext.builtin = builtin or ext.builtin
        return ext

    return _make


@pytest.fixture(autouse=True)
def _isolate_harness_configs(tmp_path, monkeypatch):
    """Backstop: every client config path in ``TOOL_SPECS`` points into
    ``tmp_path`` for every test here, so nothing in this suite can read or
    write the developer's real IDE configs even when a test forgets
    ``_point_paths_at``. Tests that need a specific path set the env var
    themselves; their monkeypatch runs after this one and wins."""
    from axiom.extensions.builtins.mcp.install import TOOL_SPECS

    for spec in TOOL_SPECS:
        monkeypatch.setenv(
            spec.env_override,
            str(tmp_path / "harness-configs" / f"{spec.name}.cfg"),
        )
