# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for the install builtin's auto-init + branding wiring.

Covers:
- First-run `load_manifest()` auto-creates `runtime/config/install.toml`
  from `runtime/config.example/install.toml` so the user does not have
  to `cp` it themselves (Bug #2 from 2026-05-19 Austin onboarding pass).
- A pre-existing customized `install.toml` is never overwritten.
- The CLI's user-facing strings honor the active BrandingConfig
  (env var name, product banner, re-run command) so a `neut`-branded
  invocation never leaks `axi` vocabulary.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from axiom.extensions.builtins.install import cli as install_cli
from axiom.extensions.builtins.install import installer as install_mod
from axiom.infra.branding import BrandingConfig, register, reset

EXAMPLE_TOML = """\
[[environments]]
name = "test-env"
description = "Test env for installer auto-init"
match_hostname = ["test-host"]
"""


@pytest.fixture
def isolated_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Point installer paths at a tmpdir so we can simulate first-run state."""

    config_dir = tmp_path / "runtime" / "config"
    example_dir = tmp_path / "runtime" / "config.example"
    state_dir = tmp_path / ".neut"
    install_toml = config_dir / "install.toml"
    example_toml = example_dir / "install.toml"
    state_path = state_dir / "install-state.json"

    example_dir.mkdir(parents=True)
    example_toml.write_text(EXAMPLE_TOML)

    monkeypatch.setattr(install_mod, "_INSTALL_TOML", install_toml)
    monkeypatch.setattr(
        install_mod, "_INSTALL_LOCAL_TOML", config_dir / "install.local.toml"
    )
    monkeypatch.setattr(install_mod, "_INSTALL_EXAMPLE_TOML", example_toml)
    monkeypatch.setattr(install_mod, "_STATE_PATH", state_path)

    return install_toml, example_toml


# ---------------------------------------------------------------------------
# Auto-init from example
# ---------------------------------------------------------------------------


def test_load_manifest_auto_inits_install_toml_from_example(isolated_paths):
    install_toml, example_toml = isolated_paths
    assert not install_toml.exists(), "fixture precondition: customized file absent"

    envs = install_mod.load_manifest()

    assert install_toml.exists(), (
        "load_manifest should auto-copy the example to config/install.toml on "
        "first run so the user does not have to `cp` it themselves"
    )
    assert install_toml.read_text() == example_toml.read_text()
    assert [e.name for e in envs] == ["test-env"]


def test_load_manifest_does_not_overwrite_customized_install_toml(isolated_paths):
    install_toml, _ = isolated_paths
    install_toml.parent.mkdir(parents=True, exist_ok=True)
    install_toml.write_text(
        '[[environments]]\nname = "custom-env"\nmatch_hostname = ["x"]\n'
    )

    envs = install_mod.load_manifest()

    # Existing file is preserved verbatim.
    assert 'name = "custom-env"' in install_toml.read_text()
    assert [e.name for e in envs] == ["custom-env"]


def test_load_manifest_returns_empty_when_neither_file_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(install_mod, "_INSTALL_TOML", tmp_path / "install.toml")
    monkeypatch.setattr(
        install_mod, "_INSTALL_LOCAL_TOML", tmp_path / "install.local.toml"
    )
    monkeypatch.setattr(
        install_mod, "_INSTALL_EXAMPLE_TOML", tmp_path / "example.toml"
    )

    assert install_mod.load_manifest() == []


# ---------------------------------------------------------------------------
# Branding wiring
# ---------------------------------------------------------------------------


@pytest.fixture
def neut_branding():
    """Activate an example consumer branding for the duration of the test."""

    register(
        BrandingConfig(
            cli_name="acme",
            product_name="Acme OS",
            mascot_name="Acme",
            package_name="acme-os",
        )
    )
    try:
        yield
    finally:
        reset()


def test_unknown_env_message_uses_brand_specific_env_var(
    isolated_paths, capsys, neut_branding
):
    # Use a hostname pattern that the current runner will never match so
    # detect_environment returns None and we hit the branded message path.
    _, example_toml = isolated_paths
    example_toml.write_text(
        '[[environments]]\nname = "no-match-env"\n'
        'match_hostname = ["__never_matches_any_real_hostname__"]\n'
    )
    rc = install_cli.main([])
    captured = capsys.readouterr()

    assert rc == 1
    assert "ACME_ENV" in captured.out, (
        f"branded env var name should be ACME_ENV under acme branding; "
        f"got output: {captured.out!r}"
    )
    assert "AXI_ENV" not in captured.out


def test_product_banner_uses_brand_product_name(
    isolated_paths, capsys, monkeypatch, neut_branding
):
    # Force a matching environment so the install banner prints.
    install_toml, _ = isolated_paths
    install_toml.parent.mkdir(parents=True, exist_ok=True)
    install_toml.write_text(
        '[[environments]]\nname = "match-env"\n'
        'description = "match-env desc"\nmatch_hostname = ["*"]\n'
    )

    # No steps → completes immediately after banner.
    monkeypatch.setattr(install_cli, "_finalize_register_agents", lambda: None)
    monkeypatch.setattr(install_cli, "_finalize_install_shim", lambda: None)

    rc = install_cli.main([])
    captured = capsys.readouterr()

    assert rc == 0
    assert "Acme OS Install" in captured.out, (
        f"banner should use brand.product_name; got output: {captured.out!r}"
    )
    assert "AcmeOS Install" not in captured.out, (
        "banner must not collapse the branded product_name"
    )
