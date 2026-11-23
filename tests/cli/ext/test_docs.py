# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Tests for ``axi ext docs`` — EXTENSION_CONTRACTS.md generator."""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from axiom.cli.ext.commands.docs import DocsProvider, render_contracts
from axiom.cli.ext.provider import CliContext


@pytest.fixture
def run_docs_cli(capsys):
    def _run(path: Path, *extra: str) -> tuple[int, str]:
        capsys.readouterr()  # discard scaffold chatter
        provider = DocsProvider()
        parser = argparse.ArgumentParser()
        provider.add_arguments(parser)
        args = parser.parse_args([str(path), *extra])
        ctx = CliContext(cwd=path)
        rc = provider.run(args, ctx)
        captured = capsys.readouterr()
        return rc, captured.out

    return _run


# ---------------------------------------------------------------------------
# Provider registration + legacy removal
# ---------------------------------------------------------------------------


def test_docs_provider_is_registered_as_builtin() -> None:
    from axiom.cli.ext.registry import discover_providers

    providers = discover_providers()
    assert "docs" in providers
    assert providers["docs"].verb == "docs"


def test_legacy_docs_subparser_removed() -> None:
    """``_cmd_docs`` is gone — the Provider owns the verb cleanly now."""
    from axiom.extensions import cli as legacy_cli

    assert not hasattr(legacy_cli, "_cmd_docs"), (
        "legacy _cmd_docs handler must be removed when the Provider takes over"
    )


# ---------------------------------------------------------------------------
# render_contracts — pure function, easy to exercise
# ---------------------------------------------------------------------------


def test_render_contracts_includes_manifest_name(scaffolded_extension) -> None:
    ext = scaffolded_extension("docgen_ext")
    text = render_contracts(ext)
    assert "docgen_ext" in text
    assert "# " in text  # has a markdown header


def test_render_contracts_lists_provides_blocks(scaffolded_extension) -> None:
    ext = scaffolded_extension("provides_ext")
    text = render_contracts(ext)
    # The scaffold emits a placeholder cmd provides block; it should surface.
    assert "cmd" in text
    assert "provides_ext" in text


def test_render_contracts_describes_public_api(scaffolded_extension) -> None:
    ext = scaffolded_extension("api_ext")
    pkg_init = ext / "api_ext" / "__init__.py"
    pkg_init.write_text(pkg_init.read_text().replace("__all__: list[str] = []", '__all__: list[str] = ["Foo", "Bar"]'))
    text = render_contracts(ext)
    assert "Foo" in text
    assert "Bar" in text


def test_render_contracts_embeds_skill_md(scaffolded_extension) -> None:
    ext = scaffolded_extension("skill_ext")
    skill_dir = ext / "skill_ext" / "skills" / "demo_skill"
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text("# demo_skill\n\nA reusable demo skill.\n")
    text = render_contracts(ext)
    assert "demo_skill" in text
    assert "reusable demo skill" in text


# ---------------------------------------------------------------------------
# Provider behavior: default write + --stdout
# ---------------------------------------------------------------------------


def test_docs_writes_to_docs_subdir_by_default(scaffolded_extension, run_docs_cli) -> None:
    ext = scaffolded_extension("write_ext")
    rc, out = run_docs_cli(ext)
    assert rc == 0
    target = ext / "docs" / "EXTENSION_CONTRACTS.md"
    assert target.exists()
    assert "write_ext" in target.read_text()


def test_docs_stdout_mode_prints_and_does_not_write(
    scaffolded_extension, run_docs_cli
) -> None:
    ext = scaffolded_extension("stdout_ext")
    rc, out = run_docs_cli(ext, "--stdout")
    assert rc == 0
    assert "stdout_ext" in out
    # --stdout must NOT write a file.
    assert not (ext / "docs" / "EXTENSION_CONTRACTS.md").exists()


def test_docs_fails_when_no_manifest(tmp_path: Path, run_docs_cli) -> None:
    broken = tmp_path / "no_manifest"
    broken.mkdir()
    rc, out = run_docs_cli(broken)
    assert rc != 0
