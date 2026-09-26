# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Tests for ``axi ext quickstart <name>`` — composite init+lint+validate+scan."""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from axiom.cli.ext.commands.quickstart import QuickstartProvider
from axiom.cli.ext.provider import CliContext


@pytest.fixture
def axiom_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "axiom_home"
    home.mkdir()
    monkeypatch.setenv("AXIOM_HOME", str(home))
    monkeypatch.delenv("AXIOM_REGISTRY_URL", raising=False)
    monkeypatch.setenv("AXIOM_INSTALL_NO_PIP", "1")
    return home


def _run(*argv: str, cwd: Path, capsys) -> tuple[int, str, str]:
    provider = QuickstartProvider()
    parser = argparse.ArgumentParser()
    provider.add_arguments(parser)
    args = parser.parse_args(list(argv))
    ctx = CliContext(cwd=cwd)
    capsys.readouterr()
    rc = provider.run(args, ctx)
    cap = capsys.readouterr()
    return rc, cap.out, cap.err


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_quickstart_provider_is_registered() -> None:
    from axiom.cli.ext.registry import discover_providers

    providers = discover_providers()
    assert "quickstart" in providers
    assert providers["quickstart"].verb == "quickstart"


def test_quickstart_is_in_scaffold_lifecycle_group() -> None:
    from axiom.extensions.cli import LIFECYCLE_GROUPS

    scaffold_verbs: tuple[str, ...] = ()
    for header, verbs in LIFECYCLE_GROUPS:
        if header == "Scaffold":
            scaffold_verbs = verbs
            break
    assert "quickstart" in scaffold_verbs


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_quickstart_happy_path(
    tmp_path: Path, axiom_home: Path, capsys
) -> None:
    rc, out, err = _run(
        "qs_ext", "--dir", str(tmp_path), cwd=tmp_path, capsys=capsys
    )
    assert rc == 0, out + err
    # The directory was created.
    assert (tmp_path / "qs_ext").is_dir()
    # Key phases should have run.
    lowered = out.lower()
    assert "scaffolded" in lowered
    assert "lint" in lowered
    assert "validate" in lowered
    assert "scan" in lowered
    # Closing guidance should mention publish.
    assert "publish" in lowered


# ---------------------------------------------------------------------------
# --publish composes
# ---------------------------------------------------------------------------


def test_quickstart_with_publish_flag(
    tmp_path: Path, axiom_home: Path, capsys
) -> None:
    rc, out, err = _run(
        "pub_ext",
        "--dir",
        str(tmp_path),
        "--publish",
        cwd=tmp_path,
        capsys=capsys,
    )
    assert rc == 0, out + err
    # Registry should now contain the extension.
    from axiom.cli.ext.registry_backend import read_index

    idx = read_index()
    assert "pub_ext" in (idx.get("extensions") or {})


# ---------------------------------------------------------------------------
# Status copy update
# ---------------------------------------------------------------------------


def test_status_welcome_mentions_quickstart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    monkeypatch.setenv("AXIOM_HOME", str(tmp_path / "empty_home"))

    from axiom.cli.ext.commands.status import StatusProvider

    provider = StatusProvider()
    parser = argparse.ArgumentParser()
    provider.add_arguments(parser)
    args = parser.parse_args([])
    ctx = CliContext(cwd=tmp_path)
    capsys.readouterr()
    rc = provider.run(args, ctx)
    out = capsys.readouterr().out
    assert rc == 0
    lowered = out.lower()
    assert "quickstart" in lowered
    # The old placeholder phrasing must not remain.
    assert "coming soon" not in lowered


# ---------------------------------------------------------------------------
# Failure propagation
# ---------------------------------------------------------------------------


def test_quickstart_aborts_on_lint_failure(
    tmp_path: Path,
    axiom_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    """Inject a forced lint failure; quickstart must stop + point to the failure."""
    from axiom.cli.ext.commands import quickstart as qs_mod
    from axiom.cli.ext.commands.lint import Finding

    def fake_lint(ext_path: Path) -> list[Finding]:
        return [
            Finding(
                code="AEOS999",
                severity="error",
                message="injected failure",
                remediation="fix the injected problem",
            )
        ]

    monkeypatch.setattr(qs_mod, "lint_extension", fake_lint)
    rc, out, err = _run(
        "fail_ext", "--dir", str(tmp_path), cwd=tmp_path, capsys=capsys
    )
    assert rc != 0
    # Should point the user at the failing verb.
    assert "lint" in (out + err).lower()
