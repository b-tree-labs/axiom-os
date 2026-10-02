# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Tests for ``axi ext install`` — registry -> local install.

Scaffolds + publishes a fresh extension to a tmp registry, then drives
``install`` against it. All pip invocations are skipped via
``AXIOM_INSTALL_NO_PIP=1``; the pip path is exercised separately via
monkeypatched ``subprocess.run``.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from axiom.cli.ext.commands.install import (
    InstallProvider,
    extensions_root,
    install_extension,
)
from axiom.cli.ext.commands.publish import publish_extension
from axiom.cli.ext.install_state import (
    InstallRecord,
    get_installed,
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


def _publish_fresh(
    scaffolded_extension,
    name: str = "greeter",
    version: str = "0.1.0",
    *,
    scaffold_dirname: str | None = None,
) -> Path:
    """Scaffold + publish a fresh extension so the registry has an artifact.

    The scaffold fixture puts each extension at ``tmp_path / <dirname>``;
    to publish a *second* version we need a different scaffold dirname.
    """
    scaffold_dirname = scaffold_dirname or name
    ext = scaffolded_extension(scaffold_dirname)
    # Rewrite manifest name + version to match the intended ext name.
    manifest = ext / "axiom-extension.toml"
    text = manifest.read_text()
    text = text.replace(f'name = "{scaffold_dirname}"', f'name = "{name}"')
    text = text.replace('version = "0.1.0"', f'version = "{version}"')
    manifest.write_text(text, encoding="utf-8")

    pyproject = ext / "pyproject.toml"
    py_text = pyproject.read_text()
    py_text = py_text.replace(f'name = "{scaffold_dirname}"', f'name = "{name}"')
    py_text = py_text.replace('version = "0.1.0"', f'version = "{version}"')
    pyproject.write_text(py_text, encoding="utf-8")

    publish_extension(ext, yes=True, skip_tag_check=True)
    return ext


def _run_install_cli(*argv: str, capsys) -> tuple[int, str]:
    provider = InstallProvider()
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


def test_install_is_registered_as_builtin() -> None:
    from axiom.cli.ext.registry import discover_providers

    providers = discover_providers()
    assert "install" in providers
    assert providers["install"].verb == "install"


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_install_full_happy_path(
    scaffolded_extension, axiom_home: Path, capsys
) -> None:
    _publish_fresh(scaffolded_extension, "greeter")
    rc, out = _run_install_cli("greeter", "--no-pip", capsys=capsys)
    assert rc == 0, out
    assert "Installed greeter 0.1.0" in out

    rec = get_installed("greeter")
    assert rec is not None
    assert rec.version == "0.1.0"
    assert Path(rec.install_path).exists()
    # Install location is under $AXIOM_HOME/extensions/.
    assert Path(rec.install_path).parent == extensions_root()
    # Artifact + signature hashes were recorded.
    assert rec.artifact_sha256
    assert rec.signature_sha256


def test_install_direct_api_returns_record(
    scaffolded_extension, axiom_home: Path
) -> None:
    _publish_fresh(scaffolded_extension, "greeter")
    rec = install_extension("greeter", no_pip=True)
    assert isinstance(rec, InstallRecord)
    assert rec.name == "greeter"
    assert rec.version == "0.1.0"


# ---------------------------------------------------------------------------
# Version selection
# ---------------------------------------------------------------------------


def test_install_at_version_syntax(
    scaffolded_extension, axiom_home: Path, capsys
) -> None:
    _publish_fresh(scaffolded_extension, "greeter", "0.1.0")
    rc, out = _run_install_cli(
        "greeter@0.1.0", "--no-pip", capsys=capsys
    )
    assert rc == 0, out
    assert "0.1.0" in out


def test_install_version_flag(
    scaffolded_extension, axiom_home: Path, capsys
) -> None:
    _publish_fresh(scaffolded_extension, "greeter")
    rc, out = _run_install_cli(
        "greeter", "--version", "0.1.0", "--no-pip", capsys=capsys
    )
    assert rc == 0, out


def test_install_missing_version_errors(
    scaffolded_extension, axiom_home: Path, capsys
) -> None:
    _publish_fresh(scaffolded_extension, "greeter")
    rc, out = _run_install_cli(
        "greeter@9.9.9", "--no-pip", capsys=capsys
    )
    assert rc == 1
    assert "not found" in out


def test_install_missing_extension_errors(
    axiom_home: Path, capsys
) -> None:
    rc, out = _run_install_cli("nope", "--no-pip", capsys=capsys)
    assert rc == 1
    assert "not found" in out
    assert "axi ext search" in out


# ---------------------------------------------------------------------------
# Duplicate install guard + --force
# ---------------------------------------------------------------------------


def test_install_same_version_twice_refuses_without_force(
    scaffolded_extension, axiom_home: Path, capsys
) -> None:
    _publish_fresh(scaffolded_extension, "greeter")
    rc, _ = _run_install_cli("greeter", "--no-pip", capsys=capsys)
    assert rc == 0
    rc, out = _run_install_cli("greeter", "--no-pip", capsys=capsys)
    assert rc == 1
    assert "already installed" in out


def test_install_force_allows_reinstall(
    scaffolded_extension, axiom_home: Path, capsys
) -> None:
    _publish_fresh(scaffolded_extension, "greeter")
    rc, _ = _run_install_cli("greeter", "--no-pip", capsys=capsys)
    assert rc == 0
    rc, out = _run_install_cli(
        "greeter", "--force", "--no-pip", capsys=capsys
    )
    assert rc == 0, out


# ---------------------------------------------------------------------------
# Dry-run
# ---------------------------------------------------------------------------


def test_install_dry_run_leaves_state_untouched(
    scaffolded_extension, axiom_home: Path, capsys
) -> None:
    _publish_fresh(scaffolded_extension, "greeter")
    rc, out = _run_install_cli(
        "greeter", "--dry-run", capsys=capsys
    )
    assert rc == 0
    assert "dry-run" in out
    assert get_installed("greeter") is None
    # No extensions dir created.
    install_path = extensions_root() / "greeter-0.1.0"
    assert not install_path.exists()


# ---------------------------------------------------------------------------
# Signature failure
# ---------------------------------------------------------------------------


def test_install_signature_failure_aborts(
    scaffolded_extension, axiom_home: Path, capsys
) -> None:
    """A corrupted artifact trips signature verification; nothing installs."""
    _publish_fresh(scaffolded_extension, "greeter")
    # Tamper with the signed artifact in the registry.
    registry = axiom_home / "registry"
    artifact = registry / "greeter" / "0.1.0" / "greeter-0.1.0.tar.gz"
    assert artifact.exists()
    artifact.write_bytes(b"TAMPERED")

    rc, out = _run_install_cli("greeter", "--no-pip", capsys=capsys)
    assert rc == 1
    assert "signature" in out.lower()
    assert get_installed("greeter") is None
    # No install dir left behind.
    install_path = extensions_root() / "greeter-0.1.0"
    assert not install_path.exists()


# ---------------------------------------------------------------------------
# pip rollback
# ---------------------------------------------------------------------------


def test_install_pip_failure_rolls_back_state(
    scaffolded_extension,
    axiom_home: Path,
    capsys,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # We want the pip path to actually run, but fail.
    monkeypatch.delenv("AXIOM_INSTALL_NO_PIP", raising=False)
    _publish_fresh(scaffolded_extension, "greeter")

    import axiom.cli.ext.commands.install as mod

    def fake_run(cmd, **kwargs):
        class _R:
            returncode = 1
            stdout = ""
            stderr = "simulated pip error"

        return _R()

    monkeypatch.setattr(mod.subprocess, "run", fake_run)

    rc, out = _run_install_cli("greeter", capsys=capsys)
    assert rc == 1
    assert "pip install failed" in out
    # State got rolled back.
    assert get_installed("greeter") is None
    install_path = extensions_root() / "greeter-0.1.0"
    assert not install_path.exists()


# ---------------------------------------------------------------------------
# Replacing an older version
# ---------------------------------------------------------------------------


def _duplicate_as_new_version(
    axiom_home: Path, name: str, old_version: str, new_version: str
) -> None:
    """Publish the same artifact under a new version number, reusing the
    existing signed tarball so signature verification still passes.

    A legitimate republish would re-sign; for this test we only need the
    registry to carry two versions, and the signature is over the raw
    tarball bytes which we do not mutate.
    """
    import json as _json
    import shutil as _sh

    from axiom.cli.ext.registry_backend import put as _put

    src = axiom_home / "registry" / name / old_version
    stage = axiom_home / "__stage" / f"{name}-{new_version}"
    stage.mkdir(parents=True, exist_ok=True)

    # The registry stores the manifest as manifest.toml; we pass that to put()
    # which copies it as manifest.toml again under the new version dir.
    manifest_src = src / "manifest.toml"
    manifest_dst = stage / "axiom-extension.toml"
    manifest_dst.write_text(
        manifest_src.read_text().replace(
            f'version = "{old_version}"', f'version = "{new_version}"'
        ),
        encoding="utf-8",
    )

    # Copy the artifact with a version-matching filename; re-sign is not
    # needed because we reuse the exact bytes (signature is over bytes,
    # not filename).
    artifact_src = src / f"{name}-{old_version}.tar.gz"
    artifact_dst = stage / f"{name}-{new_version}.tar.gz"
    _sh.copy2(artifact_src, artifact_dst)
    sig_src = src / f"{name}-{old_version}.tar.gz.sig"
    sig_dst = stage / f"{name}-{new_version}.tar.gz.sig"
    _sh.copy2(sig_src, sig_dst)

    attestation = _json.loads((src / "attestation.json").read_text(encoding="utf-8"))

    _put(name, new_version, manifest_dst, artifact_dst, sig_dst, attestation)


def test_install_newer_version_replaces_old(
    scaffolded_extension, axiom_home: Path, tmp_path: Path, capsys
) -> None:
    _publish_fresh(scaffolded_extension, "greeter", "0.1.0")
    _duplicate_as_new_version(axiom_home, "greeter", "0.1.0", "0.2.0")

    # Install 0.1.0 first.
    rc, _ = _run_install_cli(
        "greeter@0.1.0", "--no-pip", capsys=capsys
    )
    assert rc == 0
    old_path = extensions_root() / "greeter-0.1.0"
    assert old_path.exists()

    # Install 0.2.0 — should replace 0.1.0.
    rc, out = _run_install_cli(
        "greeter@0.2.0", "--no-pip", capsys=capsys
    )
    assert rc == 0, out
    rec = get_installed("greeter")
    assert rec is not None
    assert rec.version == "0.2.0"
    assert not old_path.exists()
    new_path = extensions_root() / "greeter-0.2.0"
    assert new_path.exists()


# ---------------------------------------------------------------------------
# Registry override
# ---------------------------------------------------------------------------


def test_install_registry_rejects_non_file_scheme(
    axiom_home: Path, capsys
) -> None:
    rc, out = _run_install_cli(
        "greeter", "--registry", "https://example.com/reg", capsys=capsys
    )
    assert rc == 1
    assert "file://" in out or "scheme" in out.lower()


# ---------------------------------------------------------------------------
# No-pip env var path
# ---------------------------------------------------------------------------


def test_install_no_pip_env_var_honored(
    scaffolded_extension,
    axiom_home: Path,
    capsys,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The env var alone should be enough — no --no-pip flag."""
    _publish_fresh(scaffolded_extension, "greeter")
    # Env var is already set by the fixture; verify we don't invoke pip.
    import axiom.cli.ext.commands.install as mod

    call_count = {"n": 0}

    def fake_run(*a, **kw):
        call_count["n"] += 1

        class _R:
            returncode = 0
            stdout = ""
            stderr = ""

        return _R()

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    rc, _ = _run_install_cli("greeter", capsys=capsys)
    assert rc == 0
    assert call_count["n"] == 0


