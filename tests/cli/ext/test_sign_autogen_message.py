# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""First-time publisher-identity creation explains itself."""

from __future__ import annotations

import argparse
from pathlib import Path

from axiom.cli.ext.commands.sign import SignProvider
from axiom.cli.ext.provider import CliContext


def test_first_time_sign_explains_publisher_identity(
    tmp_path: Path, scaffolded_extension, monkeypatch, capsys
) -> None:
    """With no existing keys the 3-line block explains what was created."""
    # Sandbox AXIOM_HOME so we can't reach the real user key store.
    axiom_home = tmp_path / ".axiom"
    monkeypatch.setenv("AXIOM_HOME", str(axiom_home))

    ext = scaffolded_extension("keyless_ext")

    provider = SignProvider()
    parser = argparse.ArgumentParser()
    provider.add_arguments(parser)
    args = parser.parse_args([str(ext), "--yes"])
    rc = provider.run(args, CliContext(cwd=tmp_path))
    assert rc == 0, "sign should succeed after auto-generating the key"

    out = capsys.readouterr().out
    # 3-line block substrings:
    assert "created publisher identity" in out
    assert "ed25519 key at" in out
    assert "back up the keys/ directory" in out
    # Load-bearing sha256 token still present so existing verify/publish
    # flows continue to parse it.
    assert "sha256:" in out.lower()


def test_second_sign_does_not_re_announce_identity(
    tmp_path: Path, scaffolded_extension, monkeypatch, capsys
) -> None:
    """Once the key exists the announcement block is skipped."""
    axiom_home = tmp_path / ".axiom"
    monkeypatch.setenv("AXIOM_HOME", str(axiom_home))

    ext = scaffolded_extension("second_ext")

    provider = SignProvider()
    parser = argparse.ArgumentParser()
    provider.add_arguments(parser)

    # First run — auto-generate.
    args = parser.parse_args([str(ext), "--yes"])
    assert provider.run(args, CliContext(cwd=tmp_path)) == 0
    capsys.readouterr()

    # Second run — key already on disk, no announcement expected.
    args = parser.parse_args([str(ext), "--yes"])
    assert provider.run(args, CliContext(cwd=tmp_path)) == 0
    out = capsys.readouterr().out
    assert "created publisher identity" not in out
