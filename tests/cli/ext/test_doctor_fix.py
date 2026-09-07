# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Tests for ``axi ext doctor --fix``.

Verifies auto-fix behavior for the known remediable findings:

- Missing pyproject entry point for a manifest-declared provides block.
- Missing placeholder.py module that the manifest references.
- Outdated copyright year in LICENSE.
- Missing py.typed marker.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from axiom.cli.ext.commands.doctor import DoctorProvider
from axiom.cli.ext.provider import CliContext


def _run(path: Path, *extra: str, capsys) -> tuple[int, str, str]:
    capsys.readouterr()
    provider = DoctorProvider()
    parser = argparse.ArgumentParser()
    provider.add_arguments(parser)
    args = parser.parse_args([str(path), *extra])
    ctx = CliContext(cwd=path)
    rc = provider.run(args, ctx)
    cap = capsys.readouterr()
    return rc, cap.out, cap.err


# ---------------------------------------------------------------------------
# No-op — fresh scaffold has nothing to fix
# ---------------------------------------------------------------------------


def test_fix_on_fresh_scaffold_reports_nothing_to_fix(
    scaffolded_extension, capsys
) -> None:
    ext = scaffolded_extension("fresh_ext")
    rc, out, _ = _run(ext, "--fix", "--skip-tests", capsys=capsys)
    assert rc == 0
    assert "nothing to fix" in out.lower()


# ---------------------------------------------------------------------------
# Entry point gap
# ---------------------------------------------------------------------------


def test_fix_adds_missing_entry_point(
    scaffolded_extension, capsys
) -> None:
    """Remove the [project.entry-points."axiom.commands"] block; doctor --fix restores it."""
    ext = scaffolded_extension("ep_ext")
    pyproject = ext / "pyproject.toml"
    text = pyproject.read_text()
    # Drop the entry-points line the scaffold emits for ep_ext.
    # Scaffolder writes:  ep_ext = "ep_ext.commands.placeholder:cli"
    # after the [project.entry-points."axiom.commands"] header.
    new = text.replace('ep_ext = "ep_ext.commands.placeholder:cli"\n', "")
    pyproject.write_text(new, encoding="utf-8")

    rc, out, _ = _run(ext, "--fix", "--skip-tests", capsys=capsys)
    assert rc == 0, out
    # Entry point should now be back.
    assert 'ep_ext = "ep_ext.commands.placeholder:cli"' in pyproject.read_text()


# ---------------------------------------------------------------------------
# Missing placeholder module
# ---------------------------------------------------------------------------


def test_fix_rewrites_missing_placeholder_module(
    scaffolded_extension, capsys
) -> None:
    ext = scaffolded_extension("ph_ext")
    module = ext / "ph_ext" / "commands" / "placeholder.py"
    assert module.exists()
    module.unlink()

    rc, out, _ = _run(ext, "--fix", "--skip-tests", capsys=capsys)
    assert rc == 0, out
    assert module.exists()
    content = module.read_text()
    assert "def cli" in content


# ---------------------------------------------------------------------------
# Outdated copyright year
# ---------------------------------------------------------------------------


def test_fix_bumps_copyright_year(scaffolded_extension, capsys) -> None:
    import datetime as _dt

    ext = scaffolded_extension("cy_ext")
    lic = ext / "LICENSE"
    text = lic.read_text()
    # Scaffold writes "Copyright 2026 The University of Texas at Austin and B-Tree Labs". Downgrade to 2019.
    outdated = text.replace("Copyright 2026", "Copyright 2019", 1)
    assert outdated != text
    lic.write_text(outdated, encoding="utf-8")

    rc, out, _ = _run(ext, "--fix", "--skip-tests", capsys=capsys)
    assert rc == 0, out
    current_year = _dt.datetime.now().year
    new_text = lic.read_text()
    assert f"Copyright {current_year}" in new_text
    assert "Copyright 2019" not in new_text


# ---------------------------------------------------------------------------
# Missing py.typed
# ---------------------------------------------------------------------------


def test_fix_recreates_py_typed(scaffolded_extension, capsys) -> None:
    ext = scaffolded_extension("pyt_ext")
    pyt = ext / "pyt_ext" / "py.typed"
    assert pyt.exists()
    pyt.unlink()

    rc, out, _ = _run(ext, "--fix", "--skip-tests", capsys=capsys)
    assert rc == 0, out
    assert pyt.exists()


# ---------------------------------------------------------------------------
# --dry-run
# ---------------------------------------------------------------------------


def test_fix_dry_run_does_not_write(scaffolded_extension, capsys) -> None:
    ext = scaffolded_extension("dr_ext")
    pyt = ext / "dr_ext" / "py.typed"
    pyt.unlink()

    rc, out, _ = _run(
        ext, "--fix", "--dry-run", "--skip-tests", capsys=capsys
    )
    # Should not have rewritten py.typed.
    assert not pyt.exists()
    # Narration should include the proposed fix.
    assert "py.typed" in out or "py_typed" in out
