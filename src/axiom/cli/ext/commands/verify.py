# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``axi ext verify <artifact>`` — detached ed25519 signature check.

Counterpart to :mod:`axi ext sign`. Given a path to a signed artifact, we:

1. Locate the signature (``<artifact>.sig`` by default; overridable via
   ``--sig``).
2. Resolve a public key to check against. Priority:
   a. ``--key <path>`` (explicit).
   b. ``<artifact>.attestation.json`` → ``public_key_sha256`` →
      ``$AXIOM_HOME/keys/trusted/<sha>.pub``.
   c. ``$AXIOM_HOME/keys/signing-ed25519.pub`` (self-trust).
3. Verify the signature. Exit 0 on pass, 1 on failure, printing publisher +
   timestamp + artifact hash from the attestation when available.

The verification backend is the same :mod:`axiom.cli.ext.signing` module
``sign`` writes against — ensuring the sign/verify flows cannot drift.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from axiom.cli.ext._output import console
from axiom.cli.ext.provider import CliContext
from axiom.cli.ext.signing import (
    default_public_key_path,
    load_public_key,
    trusted_keys_dir,
    verify_file,
)


@dataclass
class VerifyResult:
    """Structured outcome of a verification."""

    ok: bool
    artifact: Path
    publisher: str = ""
    published_at: str = ""
    artifact_sha256: str = ""
    key_source: str = ""
    detail: str = ""


def _read_attestation(artifact: Path) -> dict | None:
    att_path = artifact.with_suffix(artifact.suffix + ".attestation.json")
    if not att_path.exists():
        return None
    try:
        return json.loads(att_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def _resolve_public_key(
    artifact: Path, explicit: Path | None
) -> tuple[Path | None, str]:
    """Return ``(path_or_none, source_label)``.

    source labels describe which of the three resolution layers was used —
    handy in error messages.
    """
    if explicit is not None:
        if not explicit.exists():
            return None, f"explicit --key at {explicit} does not exist"
        return explicit, f"explicit --key ({explicit})"

    att = _read_attestation(artifact)
    if att is not None:
        sha = att.get("public_key_sha256")
        if sha:
            pinned = trusted_keys_dir() / f"{sha}.pub"
            if pinned.exists():
                return pinned, f"trusted store ({pinned.name})"

    default = default_public_key_path()
    if default.exists():
        return default, f"self-trust ({default})"

    return None, "no explicit key, no trusted-store pin, no self-trust key"


def verify_artifact(
    artifact: Path,
    *,
    sig_path: Path | None = None,
    key_path: Path | None = None,
) -> VerifyResult:
    """Verify ``artifact``'s detached signature.

    Returns a :class:`VerifyResult`. The caller decides the exit code
    policy — typically 0 for ``ok=True`` and 1 otherwise.
    """
    if not artifact.exists():
        return VerifyResult(
            ok=False,
            artifact=artifact,
            detail=f"artifact does not exist: {artifact}",
        )

    sig = sig_path or artifact.with_suffix(artifact.suffix + ".sig")
    if not sig.exists():
        return VerifyResult(
            ok=False,
            artifact=artifact,
            detail=f"signature file not found: {sig}",
        )

    key_resolved, source = _resolve_public_key(artifact, key_path)
    if key_resolved is None:
        return VerifyResult(
            ok=False,
            artifact=artifact,
            key_source=source,
            detail=(
                f"no public key available to verify {artifact}: {source}; "
                f"run `axi ext sign` on this host to self-trust, or drop "
                f"the publisher's pub key into $AXIOM_HOME/keys/trusted/"
            ),
        )

    try:
        pub = load_public_key(key_resolved)
    except Exception as exc:  # noqa: BLE001 — surface clearly
        return VerifyResult(
            ok=False,
            artifact=artifact,
            key_source=source,
            detail=f"could not load public key {key_resolved}: {exc}",
        )

    sig_hex = sig.read_text(encoding="utf-8").strip()
    ok = verify_file(pub, artifact, sig_hex)

    att = _read_attestation(artifact) or {}
    publisher = att.get("publisher", "")
    published_at = att.get("published_at", "")
    claimed_sha = att.get("artifact_sha256", "")

    # Extra integrity check: when the attestation claims a specific SHA,
    # compare against the real one. Mismatches are a fail even if the raw
    # signature-over-bytes call succeeded.
    if ok and claimed_sha:
        real_sha = hashlib.sha256(artifact.read_bytes()).hexdigest()
        if real_sha != claimed_sha:
            return VerifyResult(
                ok=False,
                artifact=artifact,
                publisher=publisher,
                published_at=published_at,
                artifact_sha256=real_sha,
                key_source=source,
                detail=(
                    "artifact SHA-256 does not match attestation claim "
                    f"(attestation={claimed_sha}, artifact={real_sha})"
                ),
            )

    if ok:
        return VerifyResult(
            ok=True,
            artifact=artifact,
            publisher=publisher,
            published_at=published_at,
            artifact_sha256=claimed_sha,
            key_source=source,
            detail="signature valid",
        )
    return VerifyResult(
        ok=False,
        artifact=artifact,
        publisher=publisher,
        published_at=published_at,
        artifact_sha256=claimed_sha,
        key_source=source,
        detail="signature invalid — tampered artifact or wrong key",
    )


class VerifyProvider:
    """Built-in provider for ``axi ext verify <artifact>``."""

    verb = "verify"
    description = "Verify a signed extension artifact"

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "artifact",
            help="Path to the signed artifact (tar.gz)",
        )
        parser.add_argument(
            "--sig",
            dest="sig_path",
            default=None,
            help="Path to the detached signature (default: <artifact>.sig)",
        )
        parser.add_argument(
            "--key",
            dest="key_path",
            default=None,
            help="Explicit public key to verify against (PEM)",
        )

    def run(self, args: argparse.Namespace, context: CliContext) -> int:
        artifact = Path(args.artifact)
        if not artifact.is_absolute():
            artifact = (context.cwd / artifact).resolve()
        sig_path = Path(args.sig_path) if args.sig_path else None
        if sig_path is not None and not sig_path.is_absolute():
            sig_path = (context.cwd / sig_path).resolve()
        key_path = Path(args.key_path) if args.key_path else None
        if key_path is not None and not key_path.is_absolute():
            key_path = (context.cwd / key_path).resolve()

        result = verify_artifact(artifact, sig_path=sig_path, key_path=key_path)

        con = console()
        if result.ok:
            con.print(f"axi ext verify: OK — {artifact.name}")
            if result.publisher:
                con.print(f"  publisher:      {result.publisher}")
            if result.published_at:
                con.print(f"  published_at:   {result.published_at}")
            if result.artifact_sha256:
                con.print(f"  artifact SHA:   {result.artifact_sha256}")
            if result.key_source:
                con.print(f"  key source:     {result.key_source}")
            return 0

        con.print(f"axi ext verify: FAIL — {artifact.name}")
        if result.detail:
            con.print(f"  {result.detail}")
        if result.key_source:
            con.print(f"  key source:     {result.key_source}")
        return 1


__all__ = ["VerifyProvider", "VerifyResult", "verify_artifact"]
