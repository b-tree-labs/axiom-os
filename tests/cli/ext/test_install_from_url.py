# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Tests for ``axi ext install --from-url`` — direct-artifact install.

v0.1 supports file:// only. https:// parses but emits a clear "not yet
supported" error so the flag is plumbed and test-covered even while the
remote-registry work is still ahead.
"""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

import pytest

from axiom.cli.ext.commands.install import InstallProvider
from axiom.cli.ext.commands.publish import publish_extension
from axiom.cli.ext.install_state import get_installed
from axiom.cli.ext.provider import CliContext


@pytest.fixture
def axiom_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "axiom_home"
    # Intentionally do NOT mkdir — one of the tests verifies auto-create.
    monkeypatch.setenv("AXIOM_HOME", str(home))
    monkeypatch.delenv("AXIOM_REGISTRY_URL", raising=False)
    monkeypatch.setenv("AXIOM_INSTALL_NO_PIP", "1")
    return home


def _publish_fresh(scaffolded_extension, name: str = "greeter") -> Path:
    ext = scaffolded_extension(name)
    publish_extension(ext, yes=True, skip_tag_check=True)
    return ext


def _run(*argv: str, capsys) -> tuple[int, str, str]:
    provider = InstallProvider()
    parser = argparse.ArgumentParser()
    provider.add_arguments(parser)
    args = parser.parse_args(list(argv))
    ctx = CliContext(cwd=Path.cwd())
    capsys.readouterr()
    rc = provider.run(args, ctx)
    cap = capsys.readouterr()
    return rc, cap.out, cap.err


# ---------------------------------------------------------------------------
# file:// happy path
# ---------------------------------------------------------------------------


def test_install_from_file_url_happy_path(
    scaffolded_extension, axiom_home: Path, capsys, tmp_path: Path
) -> None:
    _publish_fresh(scaffolded_extension, "greeter")
    artifact = axiom_home / "registry" / "greeter" / "0.1.0" / "greeter-0.1.0.tar.gz"
    assert artifact.exists()

    # Install from a different name to avoid collision with the registry index.
    # (The --from-url path is the test focus, not the registry.)
    rc, out, _ = _run(
        "--from-url", f"file://{artifact}", "--no-pip", capsys=capsys
    )
    assert rc == 0, out
    rec = get_installed("greeter")
    assert rec is not None
    assert rec.version == "0.1.0"
    # Narration should include the key steps.
    lowered = out.lower()
    assert "fetched" in lowered or "from-url" in lowered or "greeter" in lowered
    assert "verified" in lowered or "signature" in lowered


# ---------------------------------------------------------------------------
# signature failure
# ---------------------------------------------------------------------------


def test_install_from_file_url_signature_failure(
    scaffolded_extension, axiom_home: Path, capsys, tmp_path: Path
) -> None:
    _publish_fresh(scaffolded_extension, "greeter")
    artifact = axiom_home / "registry" / "greeter" / "0.1.0" / "greeter-0.1.0.tar.gz"
    # Tamper with the artifact so signature verification fails.
    artifact.write_bytes(b"TAMPERED")

    rc, out, err = _run(
        "--from-url", f"file://{artifact}", "--no-pip", capsys=capsys
    )
    assert rc != 0
    assert "signature" in (out + err).lower()
    assert get_installed("greeter") is None


# ---------------------------------------------------------------------------
# https:// stub
# ---------------------------------------------------------------------------


def test_install_from_https_url_emits_stub(
    axiom_home: Path, capsys
) -> None:
    rc, out, err = _run(
        "--from-url",
        "https://example.com/foo.tar.gz",
        "--no-pip",
        capsys=capsys,
    )
    assert rc == 2
    combined = (out + err).lower()
    assert "https" in combined or "not yet supported" in combined


# ---------------------------------------------------------------------------
# $AXIOM_HOME auto-create
# ---------------------------------------------------------------------------


def test_install_from_url_auto_creates_axiom_home(
    scaffolded_extension, axiom_home: Path, capsys
) -> None:
    """``$AXIOM_HOME`` auto-created on demand; narration fires.

    The fixture sets ``AXIOM_HOME`` but deliberately leaves the directory
    missing. We publish into it (which forces the dir) then wipe it and
    install from the archived artifact/key pair.
    """
    axiom_home.mkdir(exist_ok=True)
    _publish_fresh(scaffolded_extension, "greeter")
    artifact = (
        axiom_home / "registry" / "greeter" / "0.1.0" / "greeter-0.1.0.tar.gz"
    )
    assert artifact.exists()

    # Copy artifact, sig, attestation, and trust key into a side bundle so
    # the install path re-creates the home from scratch but still trusts
    # the signature.
    bundle_dir = axiom_home.parent / "bundle"
    bundle_dir.mkdir(exist_ok=True)
    shutil.copy(artifact, bundle_dir / "greeter-0.1.0.tar.gz")
    shutil.copy(
        artifact.parent / "greeter-0.1.0.tar.gz.sig",
        bundle_dir / "greeter-0.1.0.tar.gz.sig",
    )
    shutil.copy(
        artifact.parent / "attestation.json",
        bundle_dir / "greeter-0.1.0.attestation.json",
    )
    # Preserve keys so the auto-created home passes verification.
    keys_archive = axiom_home.parent / "keys_archive"
    if keys_archive.exists():
        shutil.rmtree(keys_archive)
    shutil.copytree(axiom_home / "keys", keys_archive)

    # Wipe the entire home.
    shutil.rmtree(axiom_home)
    assert not axiom_home.exists()

    # Intercept _axiom_home at every module that has already bound the name
    # from config. Install + signing both import it at module load time.
    import axiom.cli.ext.commands.config as config_mod
    import axiom.cli.ext.commands.install as install_mod
    import axiom.cli.ext.signing as signing_mod

    orig = config_mod._axiom_home

    def _patched() -> Path:
        home = orig()
        if home.exists() and not (home / "keys").exists():
            shutil.copytree(keys_archive, home / "keys")
        return home

    config_mod._axiom_home = _patched
    install_mod._axiom_home = _patched
    if hasattr(signing_mod, "_axiom_home"):
        signing_mod._axiom_home = _patched
    try:
        bundled = bundle_dir / "greeter-0.1.0.tar.gz"
        rc, out, err = _run(
            "--from-url", f"file://{bundled}", "--no-pip", capsys=capsys
        )
    finally:
        config_mod._axiom_home = orig
        install_mod._axiom_home = orig
        if hasattr(signing_mod, "_axiom_home"):
            signing_mod._axiom_home = orig

    assert rc == 0, out + err
    assert axiom_home.exists()
    assert "created" in out.lower()
