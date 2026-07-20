# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Finding: immutable claim + evidence + signature chain."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace

from axiom.vega.identity import Keypair, verify


@dataclass(frozen=True)
class Signature:
    signer: str  # principal handle, e.g. "@ben.booth:axiom"
    signature: bytes
    role: str = "author"  # author | verifier | eval-gate | node-attestation


@dataclass(frozen=True)
class Finding:
    claim: str
    evidence: tuple[str, ...]
    content_hash: str
    signatures: tuple[Signature, ...] = field(default_factory=tuple)

    def with_claim(self, claim: str) -> Finding:
        # Tampering helper for tests — does NOT recompute hash, so verify will fail.
        return replace(self, claim=claim)


def _canonical_payload(claim: str, evidence: tuple[str, ...]) -> bytes:
    return json.dumps(
        {"claim": claim, "evidence": list(evidence)}, sort_keys=True
    ).encode("utf-8")


def _hash(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def mint(
    *,
    claim: str,
    evidence: list[str],
    author_handle: str,
    author_keypair: Keypair,
) -> Finding:
    evidence_t = tuple(evidence)
    payload = _canonical_payload(claim, evidence_t)
    content_hash = _hash(payload)
    # Sign the content hash, not the raw payload, so chain verification
    # only needs the hash plus each signature.
    sig_bytes = author_keypair.sign(content_hash.encode("ascii"))
    return Finding(
        claim=claim,
        evidence=evidence_t,
        content_hash=content_hash,
        signatures=(Signature(signer=author_handle, signature=sig_bytes, role="author"),),
    )


def attest(
    finding: Finding,
    *,
    attestor_handle: str,
    attestor_keypair: Keypair,
    role: str,
) -> Finding:
    sig_bytes = attestor_keypair.sign(finding.content_hash.encode("ascii"))
    return replace(
        finding,
        signatures=finding.signatures
        + (Signature(signer=attestor_handle, signature=sig_bytes, role=role),),
    )


def verify_finding(finding: Finding, pubkeys: dict[str, bytes]) -> bool:
    """Verify (1) content hash matches claim+evidence, (2) every signature
    verifies against the supplied public key for that signer."""
    expected_hash = _hash(_canonical_payload(finding.claim, finding.evidence))
    if expected_hash != finding.content_hash:
        return False

    for sig in finding.signatures:
        pub = pubkeys.get(sig.signer)
        if pub is None:
            return False
        if not verify(pub, finding.content_hash.encode("ascii"), sig.signature):
            return False
    return True
