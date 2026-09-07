# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""PKCE (RFC 7636) — Proof Key for Code Exchange. Mandatory for every auth-code
flow (AUTH-R1): the verifier never leaves the process; the challenge is its
SHA-256. Prevents authorization-code interception."""

from __future__ import annotations

import base64
import hashlib
import secrets


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def challenge_for(verifier: str) -> str:
    """The S256 code_challenge for a given code_verifier (deterministic)."""
    return _b64url(hashlib.sha256(verifier.encode("ascii")).digest())


def generate_pkce() -> tuple[str, str]:
    """Return ``(code_verifier, code_challenge)`` — a fresh high-entropy pair."""
    verifier = _b64url(secrets.token_bytes(32))
    return verifier, challenge_for(verifier)


__all__ = ["challenge_for", "generate_pkce"]
