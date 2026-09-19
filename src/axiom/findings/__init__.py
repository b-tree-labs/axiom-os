# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Signed, content-addressed findings (ADR-021 P2).

A Finding is an immutable claim with supporting evidence, a content hash,
and an append-only chain of signatures (contributor + verifiers + eval
attestations). Rebroadcast preserves the chain; attribution fraud would
require forging an Ed25519 signature.

Wire protocol: the Finding is the atomic unit that moves across
federation boundaries. Local eval gates verify the chain before ingest.
"""

from __future__ import annotations

from axiom.findings.finding import (
    Finding,
    Signature,
    attest,
    mint,
    verify_finding,
)

__all__ = ["Finding", "Signature", "attest", "mint", "verify_finding"]
