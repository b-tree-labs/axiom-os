# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Integration tests for SPDX fuzzy matching at the CLI boundary."""

from __future__ import annotations

import argparse
import tomllib
from pathlib import Path

from axiom.cli.ext.commands.init import InitProvider
from axiom.cli.ext.commands.scan import ScanProvider
from axiom.cli.ext.provider import CliContext


def _manifest_license(ext_path: Path) -> str:
    with (ext_path / "axiom-extension.toml").open("rb") as fh:
        return tomllib.load(fh)["extension"]["license"]


def test_init_accepts_shorthand_license(tmp_path: Path) -> None:
    """`axi ext init --license apache` resolves to Apache-2.0."""
    provider = InitProvider()
    parser = argparse.ArgumentParser()
    provider.add_arguments(parser)
    args = parser.parse_args(
        ["foo", "--dir", str(tmp_path), "--license", "apache"]
    )
    assert provider.run(args, CliContext(cwd=tmp_path)) == 0
    assert _manifest_license(tmp_path / "foo") == "Apache-2.0"


def test_init_accepts_canonical_license(tmp_path: Path) -> None:
    provider = InitProvider()
    parser = argparse.ArgumentParser()
    provider.add_arguments(parser)
    args = parser.parse_args(
        ["bar", "--dir", str(tmp_path), "--license", "MIT"]
    )
    assert provider.run(args, CliContext(cwd=tmp_path)) == 0
    assert _manifest_license(tmp_path / "bar") == "MIT"


def test_init_rejects_unknown_license(tmp_path: Path, capsys) -> None:
    provider = InitProvider()
    parser = argparse.ArgumentParser()
    provider.add_arguments(parser)
    args = parser.parse_args(
        ["baz", "--dir", str(tmp_path), "--license", "Proprietary-Internal"]
    )
    rc = provider.run(args, CliContext(cwd=tmp_path))
    assert rc == 2
    captured = capsys.readouterr()
    # error goes to stderr; substring check
    combined = captured.out + captured.err
    assert "unknown license" in combined
    assert "Apache-2.0" in combined  # allowlist listed in hint


def test_scan_allow_license_accepts_shorthand(
    tmp_path: Path, scaffolded_extension, monkeypatch
) -> None:
    """`axi ext scan --allow-license apache` resolves before passing to scan."""
    ext = scaffolded_extension("acme_ext")
    # Set the manifest license to something we'll approve via the shorthand.
    manifest = ext / "axiom-extension.toml"
    text = manifest.read_text(encoding="utf-8")
    text = text.replace('license = "Apache-2.0"', 'license = "LGPL-3.0"')
    manifest.write_text(text, encoding="utf-8")

    provider = ScanProvider()
    parser = argparse.ArgumentParser()
    provider.add_arguments(parser)
    # "lgpl" resolves to "LGPL-3.0" — the scan should pass.
    args = parser.parse_args([str(ext), "--allow-license", "lgpl"])
    rc = provider.run(args, CliContext(cwd=tmp_path))
    assert rc == 0


def test_scan_allow_license_passes_through_license_ref(
    tmp_path: Path, scaffolded_extension
) -> None:
    """Non-SPDX ``LicenseRef-*`` values reach scan unchanged (expansion hook)."""
    ext = scaffolded_extension("zed_ext")
    manifest = ext / "axiom-extension.toml"
    text = manifest.read_text(encoding="utf-8")
    text = text.replace(
        'license = "Apache-2.0"', 'license = "LicenseRef-Internal"'
    )
    manifest.write_text(text, encoding="utf-8")

    provider = ScanProvider()
    parser = argparse.ArgumentParser()
    provider.add_arguments(parser)
    args = parser.parse_args(
        [str(ext), "--allow-license", "LicenseRef-Internal"]
    )
    rc = provider.run(args, CliContext(cwd=tmp_path))
    assert rc == 0
