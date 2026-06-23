# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Tests for ``axi ext migrate`` — pre-AEOS layout migration + version stub."""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from axiom.cli.ext.commands.migrate import MigrateProvider, migrate_to_aeos_layout
from axiom.cli.ext.provider import CliContext


@pytest.fixture
def run_migrate_cli(capsys):
    def _run(path: Path, *extra: str) -> tuple[int, str]:
        capsys.readouterr()
        provider = MigrateProvider()
        parser = argparse.ArgumentParser()
        provider.add_arguments(parser)
        args = parser.parse_args([str(path), *extra])
        ctx = CliContext(cwd=path)
        rc = provider.run(args, ctx)
        captured = capsys.readouterr()
        return rc, captured.out

    return _run


# ---------------------------------------------------------------------------
# Provider registration
# ---------------------------------------------------------------------------


def test_migrate_provider_is_registered_as_builtin() -> None:
    from axiom.cli.ext.registry import discover_providers

    providers = discover_providers()
    assert "migrate" in providers


# ---------------------------------------------------------------------------
# Version-to-version stub — the only real mode for AEOS 0.1
# ---------------------------------------------------------------------------


def test_migrate_version_same_version_stub(scaffolded_extension, run_migrate_cli) -> None:
    ext = scaffolded_extension("stubby_ext")
    rc, out = run_migrate_cli(ext, "--from-version", "0.1.0", "--to-version", "0.1.0")
    assert rc == 0
    assert "no migration" in out.lower()


def test_migrate_version_unsupported_combo(scaffolded_extension, run_migrate_cli) -> None:
    ext = scaffolded_extension("future_ext")
    rc, _ = run_migrate_cli(ext, "--from-version", "0.1.0", "--to-version", "0.2.0")
    assert rc != 0


# ---------------------------------------------------------------------------
# Layout migration — pre-AEOS flat layout → compound
# ---------------------------------------------------------------------------


def _write_pre_aeos_flat(ext_dir: Path) -> None:
    """Build a legacy ``foo_agent/`` shape.

    The shape supported by the migrator is:

      foo_agent/
        __init__.py
        agent.py
        cli.py
        contract.py          (optional)
        chat_tools.py        (optional)
        AGENT.md             (optional)
        SKILLS.md            (optional)
        axiom-extension.toml (optional, pre-AEOS schema)
    """
    ext_dir.mkdir(parents=True)
    (ext_dir / "__init__.py").write_text("# legacy\n")
    (ext_dir / "agent.py").write_text("class Foo:\n    pass\n")
    (ext_dir / "cli.py").write_text("def main(args):\n    return 0\n")
    (ext_dir / "contract.py").write_text("# contract\n")
    (ext_dir / "chat_tools.py").write_text("TOOLS = []\n")
    (ext_dir / "AGENT.md").write_text("# agent persona\n")
    (ext_dir / "SKILLS.md").write_text("# skills\n")


def test_migrate_to_aeos_layout_relocates_files(tmp_path: Path) -> None:
    legacy = tmp_path / "foo_agent"
    _write_pre_aeos_flat(legacy)
    summary = migrate_to_aeos_layout(legacy, target_name="foo")

    # Directory renamed (type suffix stripped).
    assert not legacy.exists()
    new_root = tmp_path / "foo"
    assert new_root.is_dir()

    # Source moved under the new package dir.
    pkg = new_root / "foo"
    assert pkg.is_dir()
    assert (pkg / "__init__.py").exists()
    assert (pkg / "agents" / "agent.py").exists()
    assert (pkg / "commands" / "cli.py").exists()
    assert (pkg / "tools" / "chat_tools.py").exists()

    # A MIGRATION.md summarizes what moved.
    migration_md = new_root / "MIGRATION.md"
    assert migration_md.exists()
    assert "agent.py" in migration_md.read_text()

    # The summary returned to the caller enumerates moves.
    moves = summary["moves"]
    assert any("agent.py" in src for src, _ in moves)


def test_migrate_to_aeos_layout_keeps_purpose_named_dirs(tmp_path: Path) -> None:
    """`connect/` already purpose-named — only layout changes, no rename."""
    legacy = tmp_path / "connect"
    _write_pre_aeos_flat(legacy)
    migrate_to_aeos_layout(legacy, target_name="connect")
    assert (tmp_path / "connect").is_dir()
    assert (tmp_path / "connect" / "connect" / "agents" / "agent.py").exists()


def test_migrate_cli_default_detects_pre_aeos(tmp_path: Path, run_migrate_cli) -> None:
    legacy = tmp_path / "baz_agent"
    _write_pre_aeos_flat(legacy)
    rc, _ = run_migrate_cli(legacy, "--to-aeos-layout")
    assert rc == 0
    assert (tmp_path / "baz" / "baz" / "agents" / "agent.py").exists()


def test_migrate_refuses_already_aeos_compound(
    scaffolded_extension, run_migrate_cli
) -> None:
    ext = scaffolded_extension("already_compound")
    rc, out = run_migrate_cli(ext, "--to-aeos-layout")
    # Already canonical — no layout move needed. Exit 0 with a clear message.
    assert rc == 0
    assert "already" in out.lower()


def test_migrate_writes_migration_md_with_changelog(tmp_path: Path) -> None:
    legacy = tmp_path / "qux_agent"
    _write_pre_aeos_flat(legacy)
    migrate_to_aeos_layout(legacy, target_name="qux")
    text = (tmp_path / "qux" / "MIGRATION.md").read_text()
    assert "AEOS" in text
    # All source files should be mentioned.
    for name in ("agent.py", "cli.py", "chat_tools.py"):
        assert name in text


# ---------------------------------------------------------------------------
# Unit 11: auto-detect + prompt
# ---------------------------------------------------------------------------


def test_migrate_plan_summary_is_printed(tmp_path: Path, run_migrate_cli) -> None:
    """Bare invocation shows a plan with the file list before acting."""
    legacy = tmp_path / "plan_agent"
    _write_pre_aeos_flat(legacy)
    # In a pytest run stdin is NOT a TTY — bare invocation should exit 2
    # and print the plan + the flag hint.
    rc, out = run_migrate_cli(legacy)
    assert rc == 2
    assert "detected pre-AEOS layout" in out
    assert "file(s) to move" in out
    assert "--yes" in out or "--to-aeos-layout" in out


def test_migrate_yes_skips_prompt_and_executes(
    tmp_path: Path, run_migrate_cli
) -> None:
    """--yes skips the prompt and performs the migration."""
    legacy = tmp_path / "yes_agent"
    _write_pre_aeos_flat(legacy)
    rc, out = run_migrate_cli(legacy, "--yes")
    assert rc == 0
    # New compound layout exists.
    assert (tmp_path / "yes" / "yes" / "agents" / "agent.py").exists()


def test_migrate_dry_run_does_not_touch_disk(
    tmp_path: Path, run_migrate_cli
) -> None:
    """--dry-run shows the plan without mutating the legacy tree."""
    legacy = tmp_path / "dry_agent"
    _write_pre_aeos_flat(legacy)
    before_children = sorted(p.name for p in legacy.iterdir())
    rc, out = run_migrate_cli(legacy, "--dry-run")
    assert rc == 0
    assert "dry-run" in out.lower()
    after_children = sorted(p.name for p in legacy.iterdir())
    assert before_children == after_children


def test_migrate_tty_prompt_accepts(
    tmp_path: Path, run_migrate_cli, monkeypatch
) -> None:
    """With stdin+stdout TTY, the prompt accepts ``y`` and proceeds."""
    legacy = tmp_path / "tty_agent"
    _write_pre_aeos_flat(legacy)

    # Force stdin / stdout to appear as TTYs for the duration of this call.
    import sys as _sys

    monkeypatch.setattr(_sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(_sys.stdout, "isatty", lambda: True, raising=False)
    monkeypatch.setattr("builtins.input", lambda *a, **kw: "y")

    rc, _ = run_migrate_cli(legacy)
    assert rc == 0
    assert (tmp_path / "tty" / "tty" / "agents" / "agent.py").exists()
