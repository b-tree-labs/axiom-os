# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Digest: signed envelope of findings for peer-to-peer exchange."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from axiom.findings import Finding, verify_finding
from axiom.vega.identity import Keypair, verify


@dataclass(frozen=True)
class Digest:
    from_node: str  # node principal handle
    to_node: str  # intended recipient (can be a wildcard/broadcast later)
    findings: tuple[Finding, ...]
    node_signature: bytes  # signature by from_node's node key over the digest hash

    def envelope_hash(self) -> str:
        payload = json.dumps(
            {
                "from": self.from_node,
                "to": self.to_node,
                "finding_hashes": [f.content_hash for f in self.findings],
            },
            sort_keys=True,
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()


def build_digest(
    *,
    findings: list[Finding],
    from_node: str,
    node_keypair: Keypair,
    to_node: str,
) -> Digest:
    findings_t = tuple(findings)
    # Compute envelope hash over finding content_hashes (not full findings) so
    # the envelope signature is stable regardless of evidence ordering within findings.
    payload = json.dumps(
        {
            "from": from_node,
            "to": to_node,
            "finding_hashes": [f.content_hash for f in findings_t],
        },
        sort_keys=True,
    ).encode("utf-8")
    envelope_hash = hashlib.sha256(payload).hexdigest()
    sig = node_keypair.sign(envelope_hash.encode("ascii"))
    return Digest(
        from_node=from_node,
        to_node=to_node,
        findings=findings_t,
        node_signature=sig,
    )


def verify_digest(digest: Digest, pubkeys: dict[str, bytes]) -> bool:
    """Verify: (1) envelope signature by from_node, (2) every finding's chain."""
    node_pub = pubkeys.get(digest.from_node)
    if node_pub is None:
        return False
    if not verify(node_pub, digest.envelope_hash().encode("ascii"), digest.node_signature):
        return False
    return all(verify_finding(f, pubkeys) for f in digest.findings)
