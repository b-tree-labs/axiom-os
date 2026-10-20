# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Tests for ``axi ext uninstall`` — inverse of install."""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from axiom.cli.ext.commands.install import extensions_root, install_extension
from axiom.cli.ext.commands.publish import publish_extension
from axiom.cli.ext.commands.uninstall import (
    UninstallProvider,
    uninstall_extension,
)
from axiom.cli.ext.install_state import (
    InstallRecord,
    get_installed,
    record_install,
)
from axiom.cli.ext.provider import CliContext


@pytest.fixture
def axiom_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "axiom_home"
    home.mkdir()
    monkeypatch.setenv("AXIOM_HOME", str(home))
    monkeypatch.delenv("AXIOM_REGISTRY_URL", raising=False)
    monkeypatch.setenv("AXIOM_INSTALL_NO_PIP", "1")
    return home


def _install_fresh(scaffolded_extension, name: str = "greeter") -> None:
    ext = scaffolded_extension(name)
    publish_extension(ext, yes=True, skip_tag_check=True)
    install_extension(name, no_pip=True)


def _run_uninstall_cli(*argv: str, capsys) -> tuple[int, str]:
    provider = UninstallProvider()
    parser = argparse.ArgumentParser()
    provider.add_arguments(parser)
    args = parser.parse_args(list(argv))
    ctx = CliContext(cwd=Path.cwd())
    capsys.readouterr()
    rc = provider.run(args, ctx)
    out = capsys.readouterr().out
    return rc, out


# ---------------------------------------------------------------------------
# Provider registration
# ---------------------------------------------------------------------------


def test_uninstall_is_registered_as_builtin() -> None:
    from axiom.cli.ext.registry import discover_providers

    providers = discover_providers()
    assert "uninstall" in providers
    assert providers["uninstall"].verb == "uninstall"


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_uninstall_removes_state_and_directory(
    scaffolded_extension, axiom_home: Path, capsys
) -> None:
    _install_fresh(scaffolded_extension, "greeter")
    assert get_installed("greeter") is not None
    install_dir = extensions_root() / "greeter-0.1.0"
    assert install_dir.exists()

    rc, out = _run_uninstall_cli("greeter", "--no-pip", capsys=capsys)
    assert rc == 0, out
    assert "Uninstalled greeter 0.1.0" in out
    assert get_installed("greeter") is None
    assert not install_dir.exists()


def test_uninstall_direct_api_returns_version(
    scaffolded_extension, axiom_home: Path
) -> None:
    _install_fresh(scaffolded_extension, "greeter")
    version = uninstall_extension("greeter", no_pip=True)
    assert version == "0.1.0"


# ---------------------------------------------------------------------------
# Not-installed
# ---------------------------------------------------------------------------


def test_uninstall_missing_exits_one(axiom_home: Path, capsys) -> None:
    rc, out = _run_uninstall_cli("nope", "--no-pip", capsys=capsys)
    assert rc == 1
    assert "not installed" in out
    assert "axi ext list" in out


# ---------------------------------------------------------------------------
# Path traversal guard
# ---------------------------------------------------------------------------


def test_uninstall_refuses_path_outside_axiom_home(
    axiom_home: Path, tmp_path: Path, capsys
) -> None:
    dodgy_dir = tmp_path / "not-under-axiom"
    dodgy_dir.mkdir()
    record_install(
        InstallRecord(
            name="evil",
            version="1.0",
            installed_at="2026-04-22T12:00:00Z",
            install_path=str(dodgy_dir),
            artifact_sha256="a",
            signature_sha256="b",
            registry_url="file:///x",
        )
    )
    rc, out = _run_uninstall_cli("evil", "--no-pip", capsys=capsys)
    assert rc == 1
    assert "refusing" in out.lower() or "outside" in out.lower()
    # Dodgy dir untouched.
    assert dodgy_dir.exists()
    # State record also untouched when the guard trips — user needs
    # to fix the state by hand (it's already malformed).
    assert get_installed("evil") is not None


# ---------------------------------------------------------------------------
# pip failure tolerance
# ---------------------------------------------------------------------------


def test_uninstall_pip_failure_still_cleans_state(
    scaffolded_extension,
    axiom_home: Path,
    capsys,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """pip uninstall failure leaves a warning but still clears state."""
    monkeypatch.delenv("AXIOM_INSTALL_NO_PIP", raising=False)
    _install_fresh(scaffolded_extension, "greeter")

    import axiom.cli.ext.commands.uninstall as mod

    def fake_run(cmd, **kwargs):
        class _R:
            returncode = 1
            stdout = ""
            stderr = "simulated pip error"

        return _R()

    monkeypatch.setattr(mod.subprocess, "run", fake_run)

    rc, out = _run_uninstall_cli("greeter", capsys=capsys)
    assert rc == 0
    assert "warning" in out.lower()
    assert get_installed("greeter") is None


# ---------------------------------------------------------------------------
# --no-pip env var honored
# ---------------------------------------------------------------------------


def test_uninstall_no_pip_env_var_honored(
    scaffolded_extension,
    axiom_home: Path,
    capsys,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fresh(scaffolded_extension, "greeter")
    import axiom.cli.ext.commands.uninstall as mod

    call_count = {"n": 0}

    def fake_run(*a, **kw):
        call_count["n"] += 1

        class _R:
            returncode = 0
            stdout = ""
            stderr = ""

        return _R()

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    rc, _ = _run_uninstall_cli("greeter", capsys=capsys)
    assert rc == 0
    assert call_count["n"] == 0


def test_uninstall_yes_flag_accepted(
    scaffolded_extension, axiom_home: Path, capsys
) -> None:
    """--yes is plumbed for future interactive flows; it must not error."""
    _install_fresh(scaffolded_extension, "greeter")
    rc, out = _run_uninstall_cli(
        "greeter", "--yes", "--no-pip", capsys=capsys
    )
    assert rc == 0, out
