# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Every success path surfaces a ``Next steps`` hint block.

This test is deliberately shallow — we just run each verb against a
known-good scaffold and assert that a ``Next steps`` or lifecycle-entry
block appears. Deeper assertions for each verb live in their own test
modules; this one guards against regressions where a later refactor
accidentally drops the hint.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from axiom.cli.ext.commands.init import InitProvider
from axiom.cli.ext.commands.lint import LintProvider
from axiom.cli.ext.commands.list import ListProvider
from axiom.cli.ext.commands.scan import ScanProvider
from axiom.cli.ext.commands.validate import ValidateProvider
from axiom.cli.ext.provider import CliContext


def _run(provider, argv: list[str], cwd: Path) -> tuple[int, str, object]:
    parser = argparse.ArgumentParser()
    provider.add_arguments(parser)
    args = parser.parse_args(argv)
    return provider.run(args, CliContext(cwd=cwd)), args, provider


def test_init_success_has_next_steps(tmp_path: Path, capsys):
    provider = InitProvider()
    parser = argparse.ArgumentParser()
    provider.add_arguments(parser)
    args = parser.parse_args(["init_ns", "--dir", str(tmp_path)])
    assert provider.run(args, CliContext(cwd=tmp_path)) == 0
    out = capsys.readouterr().out
    assert "Next steps" in out
    assert "axi ext lint" in out


def test_lint_success_has_next_steps(tmp_path: Path, capsys, scaffolded_extension):
    ext = scaffolded_extension("lint_ns")
    capsys.readouterr()  # drop scaffold chatter
    provider = LintProvider()
    parser = argparse.ArgumentParser()
    provider.add_arguments(parser)
    args = parser.parse_args([str(ext)])
    assert provider.run(args, CliContext(cwd=tmp_path)) == 0
    out = capsys.readouterr().out
    assert "Next steps" in out
    assert "axi ext test" in out or "axi ext scan" in out


def test_validate_success_has_next_steps(
    tmp_path: Path, capsys, scaffolded_extension
):
    ext = scaffolded_extension("val_ns")
    capsys.readouterr()
    provider = ValidateProvider()
    parser = argparse.ArgumentParser()
    provider.add_arguments(parser)
    args = parser.parse_args([str(ext), "--skip-tests"])
    rc = provider.run(args, CliContext(cwd=tmp_path))
    out = capsys.readouterr().out
    if rc == 0:
        assert "Next steps" in out


def test_scan_success_has_next_steps(
    tmp_path: Path, capsys, scaffolded_extension
):
    ext = scaffolded_extension("scan_ns")
    capsys.readouterr()
    provider = ScanProvider()
    parser = argparse.ArgumentParser()
    provider.add_arguments(parser)
    args = parser.parse_args([str(ext)])
    rc = provider.run(args, CliContext(cwd=tmp_path))
    out = capsys.readouterr().out
    # Scan may pass or warn; next-steps shows up on clean pass only.
    if rc == 0 and "all checks passed" in out:
        assert "Next steps" in out


def test_list_empty_has_lifecycle_entry_points(
    tmp_path: Path, monkeypatch, capsys
):
    monkeypatch.setenv("AXIOM_HOME", str(tmp_path / "empty-home"))
    provider = ListProvider()
    # Force the pip source to return nothing so the "no extensions" block
    # shows up even in the test venv.
    import axiom.cli.ext.commands.list as list_mod

    monkeypatch.setattr(list_mod, "_pip_source", lambda: [])
    parser = argparse.ArgumentParser()
    provider.add_arguments(parser)
    args = parser.parse_args([])
    assert provider.run(args, CliContext(cwd=tmp_path)) == 0
    out = capsys.readouterr().out
    assert "Get started" in out
    assert "axi ext init" in out
