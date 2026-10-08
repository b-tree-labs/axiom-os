# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Tests for ``axi ext verify`` — detached ed25519 signature check.

The verify flow is the counterpart to ``axi ext sign`` (Unit 3). It resolves
the public key in three layers: explicit ``--key``, pinned trusted-store
entry (``$AXIOM_HOME/keys/trusted/<sha>.pub``), and finally the host's own
signing pubkey (self-trust).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from axiom.cli.ext.commands.sign import sign_artifact
from axiom.cli.ext.commands.verify import VerifyProvider, verify_artifact
from axiom.cli.ext.provider import CliContext
from axiom.cli.ext.signing import (
    generate_keypair,
    trusted_keys_dir,
)


@pytest.fixture
def axiom_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "axiom_home"
    home.mkdir()
    monkeypatch.setenv("AXIOM_HOME", str(home))
    return home


def _run_sign(ext_path: Path) -> dict:
    return sign_artifact(ext_path, yes=True)


def _run_verify_cli(
    artifact: Path, *argv: str, capsys=None
) -> tuple[int, str]:
    provider = VerifyProvider()
    parser = argparse.ArgumentParser()
    provider.add_arguments(parser)
    args = parser.parse_args([str(artifact), *argv])
    ctx = CliContext(cwd=artifact.parent)
    if capsys is not None:
        capsys.readouterr()
    rc = provider.run(args, ctx)
    out = capsys.readouterr().out if capsys is not None else ""
    return rc, out


# ---------------------------------------------------------------------------
# Provider registration
# ---------------------------------------------------------------------------


def test_verify_is_registered_as_builtin() -> None:
    from axiom.cli.ext.registry import discover_providers

    providers = discover_providers()
    assert "verify" in providers
    assert providers["verify"].verb == "verify"


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_verify_passes_for_freshly_signed_artifact(
    scaffolded_extension, axiom_home: Path, capsys
) -> None:
    ext = scaffolded_extension("vgood_ext")
    sig_result = _run_sign(ext)
    rc, out = _run_verify_cli(sig_result["artifact"], capsys=capsys)
    assert rc == 0, out
    assert "OK" in out or "verified" in out.lower() or "valid" in out.lower()


def test_verify_prints_publisher_and_timestamp_from_attestation(
    scaffolded_extension, axiom_home: Path, capsys
) -> None:
    ext = scaffolded_extension("vinfo_ext")
    sig_result = _run_sign(ext)
    rc, out = _run_verify_cli(sig_result["artifact"], capsys=capsys)
    assert rc == 0
    # The attestation's publisher should surface in the output.
    att = json.loads(sig_result["attestation"].read_text())
    assert att["publisher"] in out
    # ISO8601 timestamp substring 'T' should appear.
    assert "T" in out


# ---------------------------------------------------------------------------
# Tamper detection
# ---------------------------------------------------------------------------


def test_verify_fails_when_artifact_is_tampered(
    scaffolded_extension, axiom_home: Path, capsys
) -> None:
    ext = scaffolded_extension("tamper_ext")
    sig_result = _run_sign(ext)
    artifact = sig_result["artifact"]
    # Corrupt a single byte in the middle.
    data = bytearray(artifact.read_bytes())
    data[len(data) // 2] ^= 0xFF
    artifact.write_bytes(bytes(data))
    rc, out = _run_verify_cli(artifact, capsys=capsys)
    assert rc == 1
    assert "fail" in out.lower() or "invalid" in out.lower() or "mismatch" in out.lower()


def test_verify_fails_when_sig_file_missing(
    scaffolded_extension, axiom_home: Path, capsys
) -> None:
    ext = scaffolded_extension("nosig_ext")
    sig_result = _run_sign(ext)
    artifact = sig_result["artifact"]
    sig_result["signature"].unlink()
    rc, out = _run_verify_cli(artifact, capsys=capsys)
    assert rc == 1
    assert "sig" in out.lower()


def test_verify_fails_under_wrong_key(
    scaffolded_extension, axiom_home: Path, tmp_path: Path, capsys
) -> None:
    ext = scaffolded_extension("wrongkey_ext")
    sig_result = _run_sign(ext)
    artifact = sig_result["artifact"]

    # Generate an unrelated key and pass it explicitly.
    other_priv = tmp_path / "other.pem"
    other_pub = tmp_path / "other.pub"
    generate_keypair(private_path=other_priv, public_path=other_pub)

    rc, out = _run_verify_cli(
        artifact, "--key", str(other_pub), capsys=capsys
    )
    assert rc == 1


# ---------------------------------------------------------------------------
# Key resolution priorities
# ---------------------------------------------------------------------------


def test_verify_uses_trusted_store_when_attestation_pins(
    scaffolded_extension, axiom_home: Path, tmp_path: Path, capsys
) -> None:
    # Sign with an isolated keypair.
    ext = scaffolded_extension("trusted_ext")
    foreign_priv = tmp_path / "foreign.pem"
    foreign_pub = tmp_path / "foreign.pub"
    foreign_kp = generate_keypair(
        private_path=foreign_priv, public_path=foreign_pub
    )
    sig_result = sign_artifact(
        ext,
        key_path=foreign_priv,
        public_key_path=foreign_pub,
        yes=True,
    )

    # The attestation pins foreign_kp.public_key_sha256. Remove the local
    # signing key (AXIOM_HOME default) so verify can't fall back.
    # (We never generated it in this flow.)
    from axiom.cli.ext.signing import default_private_key_path

    assert not default_private_key_path().exists()

    # Now install foreign_pub into the trusted store under its SHA filename.
    sha = foreign_kp.public_key_sha256
    tdir = trusted_keys_dir()
    tdir.mkdir(parents=True, exist_ok=True)
    (tdir / f"{sha}.pub").write_bytes(foreign_pub.read_bytes())

    rc, out = _run_verify_cli(sig_result["artifact"], capsys=capsys)
    assert rc == 0, out


def test_verify_falls_back_to_self_trust_when_no_trusted_store_entry(
    scaffolded_extension, axiom_home: Path, capsys
) -> None:
    ext = scaffolded_extension("self_ext")
    sig_result = _run_sign(ext)
    # No trusted-store entry; default signing-ed25519.pub is the same key used
    # to sign — self-trust should pick it up.
    rc, _ = _run_verify_cli(sig_result["artifact"], capsys=capsys)
    assert rc == 0


def test_verify_errors_when_no_key_can_be_resolved(
    scaffolded_extension, axiom_home: Path, tmp_path: Path, capsys
) -> None:
    ext = scaffolded_extension("noresolve_ext")
    # Sign with a foreign key; do NOT install it into trusted store.
    foreign_priv = tmp_path / "stranger.pem"
    foreign_pub = tmp_path / "stranger.pub"
    generate_keypair(private_path=foreign_priv, public_path=foreign_pub)
    sig_result = sign_artifact(
        ext,
        key_path=foreign_priv,
        public_key_path=foreign_pub,
        yes=True,
    )

    # Default $AXIOM_HOME signing key was not generated in this flow.
    from axiom.cli.ext.signing import default_public_key_path

    assert not default_public_key_path().exists()

    rc, out = _run_verify_cli(sig_result["artifact"], capsys=capsys)
    assert rc == 1
    assert "trust" in out.lower() or "key" in out.lower()


def test_verify_honors_explicit_sig_path(
    scaffolded_extension, axiom_home: Path, capsys
) -> None:
    ext = scaffolded_extension("sigpath_ext")
    sig_result = _run_sign(ext)
    # Move the sig elsewhere and pass --sig.
    moved = ext / "dist" / "renamed.sig"
    moved.write_text(sig_result["signature"].read_text())
    sig_result["signature"].unlink()

    rc, out = _run_verify_cli(
        sig_result["artifact"], "--sig", str(moved), capsys=capsys
    )
    assert rc == 0, out


# ---------------------------------------------------------------------------
# verify_artifact() direct invocation
# ---------------------------------------------------------------------------


def test_verify_artifact_returns_structured_result(
    scaffolded_extension, axiom_home: Path
) -> None:
    ext = scaffolded_extension("struct_ext")
    sig_result = _run_sign(ext)
    result = verify_artifact(sig_result["artifact"])
    assert result.ok is True
    assert result.publisher  # comes from attestation
    assert result.artifact_sha256
