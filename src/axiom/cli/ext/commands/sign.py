# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``axi ext sign`` — build and sign an extension artifact.

The v0.1 signing backend is a local ed25519 keypair in
``$AXIOM_HOME/keys/``. This satisfies Silver conformance (spec §12.2) for
internal AEOS use; Sigstore keyless OIDC plugs in later as a provider
override. All the heavy lifting lives in :mod:`axiom.cli.ext.signing`; this
module is the CLI surface.

Behavior:

1. Locate or build the artifact at ``<ext>/dist/<name>-<version>.tar.gz``.
2. Load (or, on first run, auto-generate with ``--yes``) the keypair.
3. Sign the artifact bytes with ed25519.
4. Write detached hex signature to ``<artifact>.sig``.
5. Write ``<artifact>.attestation.json`` with publisher, timestamp, artifact
   sha256, and the pinning SHA of the public key.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import tomllib
from pathlib import Path

from axiom.cli.ext._output import console, next_steps
from axiom.cli.ext.provider import CliContext
from axiom.cli.ext.signing import (
    KeyPair,
    build_artifact,
    check_private_key_permissions,
    default_private_key_path,
    default_public_key_path,
    generate_keypair,
    load_keypair,
    sha256_file,
    sign_file,
)


def _load_manifest(ext_path: Path) -> dict:
    manifest_path = ext_path / "axiom-extension.toml"
    with manifest_path.open("rb") as fh:
        return tomllib.load(fh)


def _resolve_or_generate_keypair(
    *,
    private_path: Path | None,
    public_path: Path | None,
    yes: bool,
    announce,
    prompt,
) -> KeyPair | None:
    """Load a keypair; optionally auto-generate if missing.

    Returns the :class:`KeyPair` or ``None`` if the user declined generation.
    ``announce`` and ``prompt`` are callables so tests (and non-TTY callers)
    can stub them cleanly.
    """
    priv = private_path or default_private_key_path()
    pub = public_path or default_public_key_path()

    if priv.exists() and pub.exists():
        return load_keypair(priv, pub)

    # Missing one or both — we generate a fresh pair at the default location
    # unless the user declines.
    if not yes:
        announce(
            f"No signing key at {priv}.\n"
            f"I will generate an ed25519 keypair now; its public-key "
            "SHA-256 will be printed so you can pin it."
        )
        resp = prompt("Proceed? [y/N] ")
        if resp.strip().lower() not in {"y", "yes"}:
            return None

    kp = generate_keypair(private_path=priv, public_path=pub)
    # Announce publisher-identity creation explicitly — this is the user's
    # first security-relevant artifact, so say what, where, and how to keep it.
    # The ``sha256: <hex>`` line is retained verbatim so existing tests /
    # attestation parsers that look for the substring still match.
    keys_dir = kp.private_path.parent
    announce(
        "axi ext sign: created publisher identity\n"
        f"  ed25519 key at {kp.private_path}\n"
        f"  sha256: {kp.public_key_sha256}\n"
        f"  back up the keys/ directory ({keys_dir}) to re-use this "
        "identity on another machine"
    )
    return kp


def _utc_now_iso() -> str:
    # Explicit UTC with the Z suffix; avoids aware/naive ambiguity that
    # Python's default .isoformat() would otherwise leak.
    return _dt.datetime.now(_dt.UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _write_attestation(
    attestation_path: Path,
    *,
    publisher: str,
    artifact_sha: str,
    keypair: KeyPair,
) -> dict:
    data = {
        "publisher": publisher,
        "published_at": _utc_now_iso(),
        "artifact_sha256": artifact_sha,
        "sig_algo": "ed25519",
        "public_key_sha256": keypair.public_key_sha256,
    }
    attestation_path.write_text(
        json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return data


def sign_artifact(
    ext_path: Path,
    *,
    key_path: Path | None = None,
    public_key_path: Path | None = None,
    no_build: bool = False,
    output_dir: Path | None = None,
    yes: bool = False,
    announce=None,
    prompt=None,
) -> dict:
    """End-to-end sign of an extension artifact.

    Returns a dict of ``{artifact, signature, attestation, public_key_sha256}``
    paths + the pinning hash — useful for callers (``publish`` in particular).
    Raises :class:`FileNotFoundError` / :class:`RuntimeError` on failure.
    """
    announce = announce or (lambda msg: None)
    prompt = prompt or (lambda msg: "n")

    manifest = _load_manifest(ext_path)
    ext_block = manifest.get("extension", {})
    name = ext_block.get("name") or ext_path.name
    version = ext_block.get("version", "0.0.0")
    publisher = ext_block.get("owner") or "unknown"

    artifact_dir = output_dir or (ext_path / "dist")
    artifact_path = artifact_dir / f"{name}-{version}.tar.gz"

    if no_build:
        if not artifact_path.exists():
            raise FileNotFoundError(
                f"artifact not found at {artifact_path} and --no-build was set"
            )
        # Use the existing artifact as-is (sign whatever is on disk).
    else:
        # Default: always (re)build so a ``sign`` after a source change produces
        # a fresh tarball. Callers that want to sign a specific prebuilt blob
        # pass ``--no-build``.
        artifact_path = build_artifact(ext_path, dist_dir=artifact_dir)

    kp = _resolve_or_generate_keypair(
        private_path=key_path,
        public_path=public_key_path,
        yes=yes,
        announce=announce,
        prompt=prompt,
    )
    if kp is None:
        raise RuntimeError("signing aborted by user")

    perms_ok, perms_note = check_private_key_permissions(kp.private_path)
    if not perms_ok:
        announce(f"WARNING: {perms_note}")

    sig_hex = sign_file(kp.private, artifact_path)
    sig_path = artifact_path.with_suffix(artifact_path.suffix + ".sig")
    sig_path.write_text(sig_hex, encoding="utf-8")

    att_path = artifact_path.with_suffix(artifact_path.suffix + ".attestation.json")
    artifact_sha = sha256_file(artifact_path)
    attestation = _write_attestation(
        att_path,
        publisher=publisher,
        artifact_sha=artifact_sha,
        keypair=kp,
    )

    return {
        "artifact": artifact_path,
        "signature": sig_path,
        "attestation": att_path,
        "public_key_sha256": kp.public_key_sha256,
        "artifact_sha256": artifact_sha,
        "manifest": attestation,
    }


class SignProvider:
    """Built-in provider for ``axi ext sign [<path>]``."""

    verb = "sign"
    description = "Build and sign an extension artifact with ed25519"

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "path",
            nargs="?",
            default=None,
            help="Path to the extension (default: current working directory)",
        )
        parser.add_argument(
            "--key",
            dest="key_path",
            default=None,
            help="Path to the private key (default: $AXIOM_HOME/keys/signing-ed25519.pem)",
        )
        parser.add_argument(
            "--yes",
            action="store_true",
            help="Auto-generate the signing key on first use without prompting",
        )
        parser.add_argument(
            "--no-build",
            action="store_true",
            help="Refuse to build an artifact if one does not already exist",
        )
        parser.add_argument(
            "--output-dir",
            dest="output_dir",
            default=None,
            help="Override the build output directory (default: <ext>/dist)",
        )

    def run(self, args: argparse.Namespace, context: CliContext) -> int:
        target = Path(args.path).resolve() if args.path else context.cwd
        manifest_path = target / "axiom-extension.toml"
        if not manifest_path.exists():
            print(
                f"axi ext sign: {target} does not look like an extension "
                "(no axiom-extension.toml); aborting"
            )
            return 1

        key_path = Path(args.key_path) if args.key_path else None
        # When the user provided --key, the public file is expected to live
        # alongside with a matching suffix flip (.pem -> .pub).
        public_key_path = None
        if key_path is not None:
            pub_guess = key_path.with_suffix(".pub")
            public_key_path = pub_guess if pub_guess.exists() else None

        output_dir = Path(args.output_dir) if args.output_dir else None

        def _announce(msg: str) -> None:
            print(msg)

        def _prompt(msg: str) -> str:
            try:
                return input(msg)
            except EOFError:
                return ""

        try:
            result = sign_artifact(
                target,
                key_path=key_path,
                public_key_path=public_key_path,
                no_build=args.no_build,
                output_dir=output_dir,
                yes=args.yes,
                announce=_announce,
                prompt=_prompt,
            )
        except FileNotFoundError as exc:
            print(f"axi ext sign: {exc}")
            return 1
        except RuntimeError as exc:
            print(f"axi ext sign: {exc}")
            return 1

        con = console()
        con.print(f"Signed {result['artifact']}")
        con.print(f"  signature:   {result['signature']}")
        con.print(f"  attestation: {result['attestation']}")
        con.print(f"  pub SHA-256: {result['public_key_sha256']}")
        con.print("")
        next_steps(
            [
                "axi ext verify              # Confirm the signature resolves",
                "axi ext publish             # Register with the local file:// registry",
            ]
        )
        return 0


__all__ = ["SignProvider", "sign_artifact"]
