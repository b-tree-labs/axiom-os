# Copyright (c) 2026 B-Tree Ventures, LLC
# SPDX-License-Identifier: Apache-2.0

"""Compute-receipt signing helpers — Sci Displays Pillar 2 (cross-node).

Closes the cross-NODE compute loop: the executing peer signs the
``(latex, mode, value_repr, ast_trail)`` tuple with its local Ed25519
keypair, and the originating node verifies the signature against the
peer's pubkey from the federation directory. The result: two receipts
for the same computation — one signed by laptop, one by the remote peer — both
verifying the same answer cryptographically. **Federation actually
doing federation, not a status display.**

Per ADR-027 + ADR-028:
- The signing keypair is the node's own keypair (from
  ``~/.axi/identity/private.pem``).
- Verification uses the peer's pubkey loaded from the federation
  directory (``NodeRegistry``).
- The signature is over a deterministic canonical form so both signers
  get the same content hash for the same input.
"""

from __future__ import annotations

import base64
import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SignedComputeResult:
    """Wire format for a peer-executed compute result."""

    latex: str
    mode: str
    precision: int
    value_repr: str
    ast_trail: str
    signing_node_id: str
    signing_node_display_name: str
    signing_pubkey_b64: str
    signature_b64: str          # base64(Ed25519 sig over canonical_message())
    canonical_hash: str         # sha256 of canonical_message() — convenience
    elapsed_ms: float

    def canonical_message(self) -> bytes:
        return canonical_message(self.latex, self.mode, self.value_repr, self.ast_trail)


def canonical_message(latex: str, mode: str, value_repr: str, ast_trail: str) -> bytes:
    """Build the deterministic byte string we sign / verify over.

    The canonical form is ``<mode>|<latex>|<value_repr>|<ast_trail>``
    encoded as UTF-8. Whitespace-sensitive, length-prefix-free — same
    bytes on every node for the same inputs. The receipt id used by
    the existing local-only path uses the same (mode, latex,
    value_repr) triple, so cross-node receipts can interoperate with
    locally-signed ones.
    """
    return f"{mode}|{latex}|{value_repr}|{ast_trail}".encode("utf-8")


def canonical_hash_hex(latex: str, mode: str, value_repr: str, ast_trail: str) -> str:
    return hashlib.sha256(canonical_message(latex, mode, value_repr, ast_trail)).hexdigest()


# ----------------------------------------------------------------------------
# Local signing
# ----------------------------------------------------------------------------


def load_local_signing_keypair(identity_dir: Path | None = None):
    """Load the local node's Ed25519 private key from PEM.

    Returns a ``Keypair`` (cryptography-backed). Raises ``FileNotFoundError``
    if no identity has been initialised on this node.
    """
    from cryptography.hazmat.primitives import serialization

    from axiom.vega.identity.keypair import Keypair

    identity_dir = identity_dir or (Path.home() / ".axi" / "identity")
    pem_path = identity_dir / "private.pem"
    if not pem_path.exists():
        raise FileNotFoundError(
            f"No node identity at {pem_path}. Run `axi federation init` first."
        )

    pem_bytes = pem_path.read_bytes()
    private = serialization.load_pem_private_key(pem_bytes, password=None)
    raw = private.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return Keypair.from_private_bytes(raw)


def sign_compute_result(
    *,
    latex: str,
    mode: str,
    precision: int,
    value_repr: str,
    ast_trail: str,
    elapsed_ms: float = 0.0,
    identity_dir: Path | None = None,
) -> SignedComputeResult:
    """Sign a compute result with the local node's Ed25519 key.

    Pulls the node's identity (display_name + node_id + pubkey) from
    ``identity.json`` so the signer is self-describing. The signed
    result carries everything a remote verifier needs.
    """
    from axiom.vega.federation.identity import load_identity

    identity = load_identity(identity_dir)
    if identity is None:
        raise RuntimeError(
            "No node identity loadable; run `axi federation init` first"
        )

    keypair = load_local_signing_keypair(identity_dir)
    msg = canonical_message(latex, mode, value_repr, ast_trail)
    signature = keypair.sign(msg)

    return SignedComputeResult(
        latex=latex,
        mode=mode,
        precision=precision,
        value_repr=value_repr,
        ast_trail=ast_trail,
        signing_node_id=identity.node_id,
        signing_node_display_name=identity.display_name,
        signing_pubkey_b64=identity.public_key,
        signature_b64=base64.b64encode(signature).decode("ascii"),
        canonical_hash=hashlib.sha256(msg).hexdigest(),
        elapsed_ms=elapsed_ms,
    )


# ----------------------------------------------------------------------------
# Verification
# ----------------------------------------------------------------------------


@dataclass(frozen=True)
class SignatureVerification:
    valid: bool
    signing_node_id: str
    signing_display_name: str
    canonical_hash: str
    reason: str = ""           # only populated on invalid


def verify_signed_result(
    result: SignedComputeResult,
    *,
    expected_node_id: str | None = None,
    expected_pubkey_b64: str | None = None,
) -> SignatureVerification:
    """Verify a peer's signature without re-running the computation.

    - ``expected_pubkey_b64`` (preferred): the pubkey the verifier
      *expects* this peer to be using (typically loaded from
      ``NodeRegistry`` for the named peer). If supplied, we check it
      matches the pubkey carried in the signed result AND verify the
      signature against it. Mismatch → invalid (defends against a peer
      lying about its identity).
    - ``expected_node_id`` (optional): same idea for node_id.
    - If neither expectation is supplied, we trust the signed result's
      self-declared pubkey and only verify the signature is internally
      consistent. Useful for cross-checking without registry access.
    """
    from axiom.vega.identity.keypair import verify

    if expected_pubkey_b64 and expected_pubkey_b64 != result.signing_pubkey_b64:
        return SignatureVerification(
            valid=False,
            signing_node_id=result.signing_node_id,
            signing_display_name=result.signing_node_display_name,
            canonical_hash=result.canonical_hash,
            reason=(
                f"signing pubkey mismatch: result claims "
                f"{result.signing_pubkey_b64[:16]}... but registry has "
                f"{expected_pubkey_b64[:16]}..."
            ),
        )
    if expected_node_id and expected_node_id != result.signing_node_id:
        return SignatureVerification(
            valid=False,
            signing_node_id=result.signing_node_id,
            signing_display_name=result.signing_node_display_name,
            canonical_hash=result.canonical_hash,
            reason=(
                f"signing node_id mismatch: result claims "
                f"{result.signing_node_id} but registry has {expected_node_id}"
            ),
        )

    try:
        pubkey_bytes = base64.b64decode(result.signing_pubkey_b64)
        sig_bytes = base64.b64decode(result.signature_b64)
        msg = canonical_message(
            result.latex, result.mode, result.value_repr, result.ast_trail
        )
        ok = verify(pubkey_bytes, msg, sig_bytes)
    except Exception as exc:
        return SignatureVerification(
            valid=False,
            signing_node_id=result.signing_node_id,
            signing_display_name=result.signing_node_display_name,
            canonical_hash=result.canonical_hash,
            reason=f"signature decode/verify error: {exc}",
        )

    return SignatureVerification(
        valid=ok,
        signing_node_id=result.signing_node_id,
        signing_display_name=result.signing_node_display_name,
        canonical_hash=result.canonical_hash,
        reason="" if ok else "Ed25519 signature did not verify against pubkey",
    )


__all__ = [
    "SignatureVerification",
    "SignedComputeResult",
    "canonical_hash_hex",
    "canonical_message",
    "load_local_signing_keypair",
    "sign_compute_result",
    "verify_signed_result",
]
