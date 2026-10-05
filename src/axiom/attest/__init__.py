# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Attestation: the platform record of accountable human acts (ADR-142).

This package holds the parts every logbook shares and that an outside verifier
must be able to reproduce: the canonical form (``canonical``) and the signed
hash chain (``chain``, ADR-143). ``tools/verify.py`` is the standalone,
standard-library-only verifier that ships in evidence packages.
"""

from __future__ import annotations

from axiom.attest.canonical import (
    SIGNATURE_FIELDS,
    CanonicalError,
    canonical_bytes,
    digest,
    normalise,
)
from axiom.attest.chain import (
    GENESIS,
    SIGNING_DOMAIN,
    Ed25519Signer,
    Signer,
    VerifyReport,
    seal,
    signing_message,
    verify_chain,
)

__all__ = [
    "GENESIS",
    "SIGNATURE_FIELDS",
    "SIGNING_DOMAIN",
    "CanonicalError",
    "Ed25519Signer",
    "Signer",
    "VerifyReport",
    "canonical_bytes",
    "digest",
    "normalise",
    "seal",
    "signing_message",
    "verify_chain",
]
