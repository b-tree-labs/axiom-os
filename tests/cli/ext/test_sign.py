# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Tests for ``axi ext sign`` + the shared :mod:`axiom.cli.ext.signing` module.

Verify helpers are imported from the same shared module so tests exercise the
exact round-trip used by :mod:`axi ext verify` in Unit 4.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
from pathlib import Path

import pytest

from axiom.cli.ext.commands.sign import SignProvider, sign_artifact
from axiom.cli.ext.provider import CliContext
from axiom.cli.ext.signing import (
    build_artifact,
    default_private_key_path,
    default_public_key_path,
    generate_keypair,
    load_keypair,
    sha256_file,
    sign_file,
    verify_file,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def axiom_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "axiom_home"
    home.mkdir()
    monkeypatch.setenv("AXIOM_HOME", str(home))
    return home


def _run_sign_cli(
    ext_path: Path, *argv: str, capsys=None, stdin_responses: list[str] | None = None
) -> tuple[int, str]:
    """Invoke :class:`SignProvider.run` with capsys capture.

    ``stdin_responses`` lets tests stub the prompt for key generation.
    """
    import builtins

    provider = SignProvider()
    parser = argparse.ArgumentParser()
    provider.add_arguments(parser)
    args = parser.parse_args([str(ext_path), *argv])
    ctx = CliContext(cwd=ext_path.parent)

    # Stub input() if the test needs to answer the y/n prompt.
    real_input = builtins.input
    if stdin_responses is not None:
        queue = list(stdin_responses)
        builtins.input = lambda prompt="": queue.pop(0) if queue else ""  # type: ignore[assignment]
    try:
        if capsys is not None:
            capsys.readouterr()
        rc = provider.run(args, ctx)
        out = capsys.readouterr().out if capsys is not None else ""
    finally:
        builtins.input = real_input  # type: ignore[assignment]
    return rc, out


# ---------------------------------------------------------------------------
# Provider registration
# ---------------------------------------------------------------------------


def test_sign_is_registered_as_builtin() -> None:
    from axiom.cli.ext.registry import discover_providers

    providers = discover_providers()
    assert "sign" in providers
    assert providers["sign"].verb == "sign"


# ---------------------------------------------------------------------------
# Key lifecycle
# ---------------------------------------------------------------------------


def test_generate_keypair_creates_files_with_restrictive_perms(
    axiom_home: Path,
) -> None:
    kp = generate_keypair()
    assert kp.private_path.exists()
    assert kp.public_path.exists()
    if os.name == "posix":
        mode = stat.S_IMODE(kp.private_path.stat().st_mode)
        assert mode == 0o600, oct(mode)
    # SHA is a well-formed hex of length 64.
    assert len(kp.public_key_sha256) == 64
    int(kp.public_key_sha256, 16)


def test_load_keypair_round_trips_with_generated(axiom_home: Path) -> None:
    kp = generate_keypair()
    loaded = load_keypair()
    # The private key objects won't compare equal but signing under each
    # produces a valid sig verifiable by either public key.
    sig = sign_file(kp.private, kp.public_path)
    assert verify_file(loaded.public, kp.public_path, sig)


# ---------------------------------------------------------------------------
# Artifact build
# ---------------------------------------------------------------------------


def test_build_artifact_creates_tarball_under_dist(
    scaffolded_extension, axiom_home: Path
) -> None:
    ext = scaffolded_extension("build_ext")
    artifact = build_artifact(ext)
    assert artifact.exists()
    assert artifact.name == "build_ext-0.1.0.tar.gz"
    assert artifact.parent == ext / "dist"


def test_build_artifact_excludes_tests_and_pycache(
    scaffolded_extension, axiom_home: Path, tmp_path: Path
) -> None:
    import tarfile

    ext = scaffolded_extension("excl_ext")
    # Drop a pretend bytecode cache and verify it's not in the tarball.
    pycache = ext / "excl_ext" / "__pycache__"
    pycache.mkdir()
    (pycache / "a.pyc").write_bytes(b"bytecode")

    artifact = build_artifact(ext)
    with tarfile.open(artifact, "r:gz") as tar:
        members = [m.name for m in tar.getmembers()]

    assert all("__pycache__" not in m for m in members)
    assert all("/tests/" not in m for m in members)
    # Sanity: the main package is present.
    assert any(m.startswith("excl_ext-0.1.0/excl_ext/") for m in members)


# ---------------------------------------------------------------------------
# Signing (end-to-end)
# ---------------------------------------------------------------------------


def test_sign_writes_detached_sig_and_attestation(
    scaffolded_extension, axiom_home: Path, capsys
) -> None:
    ext = scaffolded_extension("sig_ext")
    rc, out = _run_sign_cli(ext, "--yes", capsys=capsys)
    assert rc == 0, out

    artifact = ext / "dist" / "sig_ext-0.1.0.tar.gz"
    sig_path = artifact.with_suffix(artifact.suffix + ".sig")
    att_path = artifact.with_suffix(artifact.suffix + ".attestation.json")
    assert artifact.exists()
    assert sig_path.exists()
    assert att_path.exists()

    att = json.loads(att_path.read_text())
    assert att["sig_algo"] == "ed25519"
    assert att["artifact_sha256"] == sha256_file(artifact)
    assert len(att["public_key_sha256"]) == 64
    assert "published_at" in att
    assert att["publisher"]  # from manifest owner


def test_sign_signature_roundtrips_through_verify(
    scaffolded_extension, axiom_home: Path, capsys
) -> None:
    ext = scaffolded_extension("round_ext")
    rc, _ = _run_sign_cli(ext, "--yes", capsys=capsys)
    assert rc == 0
    kp = load_keypair()
    artifact = ext / "dist" / "round_ext-0.1.0.tar.gz"
    sig_path = artifact.with_suffix(artifact.suffix + ".sig")
    sig_hex = sig_path.read_text().strip()
    assert verify_file(kp.public, artifact, sig_hex)


def test_sign_signature_changes_when_artifact_changes(
    scaffolded_extension, axiom_home: Path, capsys
) -> None:
    import time

    ext = scaffolded_extension("mutate_ext")
    rc, _ = _run_sign_cli(ext, "--yes", capsys=capsys)
    assert rc == 0
    artifact = ext / "dist" / "mutate_ext-0.1.0.tar.gz"
    sig_path = artifact.with_suffix(artifact.suffix + ".sig")
    first_sig = sig_path.read_text().strip()
    first_artifact_bytes = artifact.read_bytes()

    # Mutate a source file. Add substantive new content and bump mtime by two
    # seconds so tarball file records change regardless of filesystem
    # granularity.
    target_py = ext / "mutate_ext" / "commands" / "placeholder.py"
    target_py.parent.mkdir(parents=True, exist_ok=True)
    target_py.write_text(
        "def cli():\n    return 'sentinel-value-that-was-not-there-before'\n",
        encoding="utf-8",
    )
    future = time.time() + 2
    os.utime(target_py, (future, future))

    rc, _ = _run_sign_cli(ext, "--yes", capsys=capsys)
    assert rc == 0
    second_sig = sig_path.read_text().strip()
    second_artifact_bytes = artifact.read_bytes()

    # Sanity: the rebuild actually produced a different tarball.
    assert first_artifact_bytes != second_artifact_bytes, (
        "tarball bytes unchanged despite source mutation — "
        "rebuild logic regression?"
    )
    # ed25519 is deterministic over the message; different bytes -> different sig.
    assert first_sig != second_sig


def test_sign_no_build_with_missing_artifact_fails(
    scaffolded_extension, axiom_home: Path, capsys
) -> None:
    ext = scaffolded_extension("nobuild_ext")
    rc, out = _run_sign_cli(ext, "--yes", "--no-build", capsys=capsys)
    assert rc == 1
    assert "no-build" in out.lower() or "not found" in out.lower()


def test_sign_honors_custom_key_path(
    scaffolded_extension, axiom_home: Path, tmp_path: Path, capsys
) -> None:
    # Generate a key in a custom location; pass it via --key.
    custom_priv = tmp_path / "custom.pem"
    custom_pub = tmp_path / "custom.pub"
    generate_keypair(private_path=custom_priv, public_path=custom_pub)

    ext = scaffolded_extension("custom_key_ext")
    rc, _ = _run_sign_cli(
        ext, "--yes", "--key", str(custom_priv), capsys=capsys
    )
    assert rc == 0

    artifact = ext / "dist" / "custom_key_ext-0.1.0.tar.gz"
    sig_path = artifact.with_suffix(artifact.suffix + ".sig")
    sig_hex = sig_path.read_text().strip()

    from axiom.cli.ext.signing import load_public_key

    pub = load_public_key(custom_pub)
    assert verify_file(pub, artifact, sig_hex)


# ---------------------------------------------------------------------------
# Key auto-generation prompt
# ---------------------------------------------------------------------------


def test_sign_prompts_when_yes_not_passed_and_user_declines(
    scaffolded_extension, axiom_home: Path, capsys
) -> None:
    ext = scaffolded_extension("decline_ext")
    rc, out = _run_sign_cli(ext, capsys=capsys, stdin_responses=["n"])
    assert rc == 1  # user declined -> abort
    # No key was written.
    assert not default_private_key_path().exists()


def test_sign_generates_key_when_user_says_yes(
    scaffolded_extension, axiom_home: Path, capsys
) -> None:
    ext = scaffolded_extension("consent_ext")
    rc, out = _run_sign_cli(ext, capsys=capsys, stdin_responses=["y"])
    assert rc == 0
    assert default_private_key_path().exists()
    assert default_public_key_path().exists()
    assert "sha256" in out.lower()


# ---------------------------------------------------------------------------
# sign_artifact() direct invocation
# ---------------------------------------------------------------------------


def test_sign_artifact_returns_full_result(
    scaffolded_extension, axiom_home: Path
) -> None:
    ext = scaffolded_extension("direct_sign_ext")
    result = sign_artifact(ext, yes=True)
    assert result["artifact"].exists()
    assert result["signature"].exists()
    assert result["attestation"].exists()
    assert len(result["public_key_sha256"]) == 64
    assert len(result["artifact_sha256"]) == 64
