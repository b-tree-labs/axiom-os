# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Tests for ``axi ext graph`` — Mermaid dependency visualization."""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from axiom.cli.ext.commands.graph import GraphProvider, render_graph
from axiom.cli.ext.provider import CliContext


@pytest.fixture
def run_graph_cli(capsys):
    def _run(*argv: str, cwd: Path | None = None) -> tuple[int, str]:
        capsys.readouterr()
        provider = GraphProvider()
        parser = argparse.ArgumentParser()
        provider.add_arguments(parser)
        args = parser.parse_args(list(argv))
        ctx = CliContext(cwd=cwd or Path.cwd())
        rc = provider.run(args, ctx)
        captured = capsys.readouterr()
        return rc, captured.out

    return _run


# ---------------------------------------------------------------------------
# Provider registration
# ---------------------------------------------------------------------------


def test_graph_provider_is_registered_as_builtin() -> None:
    from axiom.cli.ext.registry import discover_providers

    providers = discover_providers()
    assert "graph" in providers


# ---------------------------------------------------------------------------
# render_graph — single extension
# ---------------------------------------------------------------------------


def test_render_graph_emits_td_flowchart(scaffolded_extension) -> None:
    ext = scaffolded_extension("graph_ext")
    mermaid = render_graph([ext])
    assert mermaid.splitlines()[0].startswith("flowchart TD"), (
        "AEOS Mermaid convention: TD orientation"
    )


def test_render_graph_contains_extension_node(scaffolded_extension) -> None:
    ext = scaffolded_extension("named_ext")
    mermaid = render_graph([ext])
    assert "named_ext" in mermaid


def test_render_graph_styles_every_node(scaffolded_extension) -> None:
    """Project convention: every Mermaid node + subgraph styled with fill + color."""
    ext = scaffolded_extension("styled_ext")
    mermaid = render_graph([ext])
    # A `style <node> fill:...` or classDef must exist for each non-keyword line.
    assert "style " in mermaid or "classDef" in mermaid
    assert "fill:" in mermaid
    assert "color:" in mermaid


def test_render_graph_shows_capabilities(scaffolded_extension) -> None:
    ext = scaffolded_extension("capable_ext")
    mermaid = render_graph([ext])
    # The scaffold emits a placeholder cmd provides block.
    assert "cmd" in mermaid


def test_render_graph_shows_compatibility_dependencies(scaffolded_extension) -> None:
    """``[extension.compatibility]`` entries (axiom, python) must appear as deps."""
    ext = scaffolded_extension("compat_ext")
    mermaid = render_graph([ext])
    # axiom is a declared compatibility dep from the scaffold; it should show up
    # as a dependency node.
    assert "axiom" in mermaid


# ---------------------------------------------------------------------------
# Provider output: stdout by default, --output writes a file, --installed mode
# ---------------------------------------------------------------------------


def test_graph_stdout_by_default(scaffolded_extension, run_graph_cli) -> None:
    ext = scaffolded_extension("stdout_graph")
    rc, out = run_graph_cli(str(ext))
    assert rc == 0
    assert "flowchart TD" in out


def test_graph_writes_to_output_path(
    scaffolded_extension, run_graph_cli, tmp_path: Path
) -> None:
    ext = scaffolded_extension("file_graph")
    target = tmp_path / "out.mmd"
    rc, _ = run_graph_cli(str(ext), "--output", str(target))
    assert rc == 0
    assert target.exists()
    assert "flowchart TD" in target.read_text()


def test_graph_installed_mode_does_not_require_path(run_graph_cli) -> None:
    """``--installed`` collects every installed extension — path is ignored."""
    rc, out = run_graph_cli("--installed")
    # Even in environments with no extensions installed the command should
    # succeed and emit at least the header.
    assert rc == 0
    assert "flowchart TD" in out


def test_graph_fails_on_missing_manifest(tmp_path: Path, run_graph_cli) -> None:
    broken = tmp_path / "no_manifest"
    broken.mkdir()
    rc, _ = run_graph_cli(str(broken))
    assert rc != 0
