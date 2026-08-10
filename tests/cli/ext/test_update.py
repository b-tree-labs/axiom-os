# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Tests for ``axi ext update`` — refresh installs to the registry's latest."""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

import pytest

from axiom.cli.ext.commands.install import extensions_root, install_extension
from axiom.cli.ext.commands.publish import publish_extension
from axiom.cli.ext.commands.update import (
    UpdateProvider,
    plan_updates,
    update_extensions,
)
from axiom.cli.ext.install_state import get_installed
from axiom.cli.ext.provider import CliContext
from axiom.cli.ext.registry_backend import put as registry_put


@pytest.fixture
def axiom_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "axiom_home"
    home.mkdir()
    monkeypatch.setenv("AXIOM_HOME", str(home))
    monkeypatch.delenv("AXIOM_REGISTRY_URL", raising=False)
    monkeypatch.setenv("AXIOM_INSTALL_NO_PIP", "1")
    return home


def _publish_and_install(scaffolded_extension, name: str) -> None:
    ext = scaffolded_extension(name)
    publish_extension(ext, yes=True, skip_tag_check=True)
    install_extension(name, no_pip=True)


def _publish_new_version(axiom_home: Path, name: str, old: str, new: str) -> None:
    """Duplicate the registry artifact under a new version number.

    The signature is over bytes, not filename — reusing the artifact is
    safe for test-scope update plumbing.
    """
    src = axiom_home / "registry" / name / old
    stage = axiom_home / "__stage" / f"{name}-{new}"
    stage.mkdir(parents=True, exist_ok=True)

    manifest_dst = stage / "axiom-extension.toml"
    manifest_dst.write_text(
        (src / "manifest.toml")
        .read_text()
        .replace(f'version = "{old}"', f'version = "{new}"'),
        encoding="utf-8",
    )
    shutil.copy2(
        src / f"{name}-{old}.tar.gz", stage / f"{name}-{new}.tar.gz"
    )
    shutil.copy2(
        src / f"{name}-{old}.tar.gz.sig",
        stage / f"{name}-{new}.tar.gz.sig",
    )
    attestation = json.loads(
        (src / "attestation.json").read_text(encoding="utf-8")
    )
    registry_put(
        name,
        new,
        manifest_dst,
        stage / f"{name}-{new}.tar.gz",
        stage / f"{name}-{new}.tar.gz.sig",
        attestation,
    )


def _run_update_cli(*argv: str, capsys) -> tuple[int, str]:
    provider = UpdateProvider()
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


def test_update_is_registered_as_builtin() -> None:
    from axiom.cli.ext.registry import discover_providers

    providers = discover_providers()
    assert "update" in providers
    assert providers["update"].verb == "update"


# ---------------------------------------------------------------------------
# No-op (already latest)
# ---------------------------------------------------------------------------


def test_update_no_op_when_already_latest(
    scaffolded_extension, axiom_home: Path, capsys
) -> None:
    _publish_and_install(scaffolded_extension, "greeter")
    rc, out = _run_update_cli("--no-pip", capsys=capsys)
    assert rc == 0
    assert "up to date" in out.lower()


def test_plan_updates_returns_empty_when_no_updates(
    scaffolded_extension, axiom_home: Path
) -> None:
    _publish_and_install(scaffolded_extension, "greeter")
    assert plan_updates() == []


# ---------------------------------------------------------------------------
# Single target
# ---------------------------------------------------------------------------


def test_update_single_extension(
    scaffolded_extension, axiom_home: Path, capsys
) -> None:
    _publish_and_install(scaffolded_extension, "greeter")
    _publish_new_version(axiom_home, "greeter", "0.1.0", "0.2.0")

    plans = plan_updates()
    assert len(plans) == 1
    assert plans[0].new_version == "0.2.0"

    rc, out = _run_update_cli("greeter", "--no-pip", capsys=capsys)
    assert rc == 0, out
    assert "Updated greeter" in out
    rec = get_installed("greeter")
    assert rec is not None
    assert rec.version == "0.2.0"
    # New install dir exists; old one is gone.
    assert (extensions_root() / "greeter-0.2.0").exists()
    assert not (extensions_root() / "greeter-0.1.0").exists()


# ---------------------------------------------------------------------------
# Update all
# ---------------------------------------------------------------------------


def test_update_all_walks_every_installed(
    scaffolded_extension, axiom_home: Path, capsys
) -> None:
    _publish_and_install(scaffolded_extension, "first")
    _publish_and_install(scaffolded_extension, "second")
    _publish_new_version(axiom_home, "first", "0.1.0", "0.2.0")
    _publish_new_version(axiom_home, "second", "0.1.0", "0.3.0")

    rc, out = _run_update_cli("--no-pip", capsys=capsys)
    assert rc == 0, out
    assert get_installed("first").version == "0.2.0"
    assert get_installed("second").version == "0.3.0"


# ---------------------------------------------------------------------------
# Partial failure stops the run
# ---------------------------------------------------------------------------


def test_update_stops_on_first_failure_reports_partial(
    scaffolded_extension,
    axiom_home: Path,
    capsys,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _publish_and_install(scaffolded_extension, "first")
    _publish_and_install(scaffolded_extension, "second")
    _publish_new_version(axiom_home, "first", "0.1.0", "0.2.0")
    _publish_new_version(axiom_home, "second", "0.1.0", "0.3.0")

    # Sabotage install_extension for "second" only. The update loop
    # processes targets in list_installed() order — which is sorted by
    # name — so "first" should update and then "second" fail.
    import axiom.cli.ext.commands.update as mod

    real_install = mod.install_extension

    def flaky_install(name, **kwargs):
        if name == "second":
            raise RuntimeError("simulated install break")
        return real_install(name, **kwargs)

    monkeypatch.setattr(mod, "install_extension", flaky_install)

    rc, out = _run_update_cli("--no-pip", capsys=capsys)
    assert rc == 1
    assert "Updated first" in out
    assert "FAILED second" in out
    # first updated cleanly.
    assert get_installed("first").version == "0.2.0"
    # Second's update was interrupted after uninstall but before install;
    # its state row is gone (the user must re-install). This is the
    # documented trade-off of the uninstall -> install ordering.
    assert get_installed("second") is None


# ---------------------------------------------------------------------------
# Dry-run
# ---------------------------------------------------------------------------


def test_update_dry_run_prints_plan_without_executing(
    scaffolded_extension, axiom_home: Path, capsys
) -> None:
    _publish_and_install(scaffolded_extension, "greeter")
    _publish_new_version(axiom_home, "greeter", "0.1.0", "0.2.0")

    rc, out = _run_update_cli("--dry-run", capsys=capsys)
    assert rc == 0
    assert "dry-run" in out.lower()
    assert "greeter: 0.1.0 -> 0.2.0" in out
    # Nothing actually changed.
    assert get_installed("greeter").version == "0.1.0"


def test_update_dry_run_no_updates(
    scaffolded_extension, axiom_home: Path, capsys
) -> None:
    _publish_and_install(scaffolded_extension, "greeter")
    rc, out = _run_update_cli("--dry-run", capsys=capsys)
    assert rc == 0
    assert "up to date" in out.lower()


# ---------------------------------------------------------------------------
# Nonexistent target
# ---------------------------------------------------------------------------


def test_update_nonexistent_target_errors(
    axiom_home: Path, capsys
) -> None:
    rc, out = _run_update_cli("nope", "--no-pip", capsys=capsys)
    assert rc == 1
    assert "not installed" in out


# ---------------------------------------------------------------------------
# Registry override
# ---------------------------------------------------------------------------


def test_update_registry_rejects_non_file_scheme(
    axiom_home: Path, capsys
) -> None:
    rc, out = _run_update_cli(
        "--registry", "https://example.com/reg", capsys=capsys
    )
    assert rc == 1
    assert "file://" in out or "scheme" in out.lower()


# ---------------------------------------------------------------------------
# Direct API
# ---------------------------------------------------------------------------


def test_update_extensions_direct_returns_outcome(
    scaffolded_extension, axiom_home: Path
) -> None:
    _publish_and_install(scaffolded_extension, "greeter")
    _publish_new_version(axiom_home, "greeter", "0.1.0", "0.2.0")

    outcome = update_extensions(no_pip=True)
    assert [p.name for p in outcome.updated] == ["greeter"]
    assert outcome.failed == []
