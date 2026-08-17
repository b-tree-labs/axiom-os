# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""PKCE (RFC 7636) — the possession proof that makes public clients safe.

OAuth 2.1 mandates PKCE on the authorization-code grant and forbids the ``plain``
method, so this module accepts **S256 only**. The authorize request commits to a
``code_challenge``; the token exchange presents the ``code_verifier``; here we
recompute the challenge and compare in constant time. A mismatch is
``invalid_grant`` — the exchanged code cannot be redeemed without the verifier.
"""

from __future__ import annotations

import base64
import hashlib
import hmac

from .errors import INVALID_GRANT, INVALID_REQUEST, OAuthError

#: The only challenge method OAuth 2.1 permits.
S256 = "S256"

# RFC 7636 §4.1: the verifier is 43–128 chars from the unreserved set.
_MIN_VERIFIER_LEN = 43
_MAX_VERIFIER_LEN = 128


def compute_s256_challenge(verifier: str) -> str:
    """BASE64URL(SHA256(verifier)) with no padding (RFC 7636 §4.6)."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def verify_pkce(verifier: str | None, challenge: str, method: str) -> None:
    """Verify a ``code_verifier`` against the stored ``code_challenge``.

    Raises :class:`OAuthError` (``invalid_request`` for a non-S256 method,
    ``invalid_grant`` for a missing / malformed / mismatched verifier) and
    returns ``None`` on success.
    """
    if method != S256:
        raise OAuthError(
            INVALID_REQUEST, "unsupported code_challenge_method (S256 required)"
        )
    if not verifier:
        raise OAuthError(INVALID_GRANT, "code_verifier required")
    if not (_MIN_VERIFIER_LEN <= len(verifier) <= _MAX_VERIFIER_LEN):
        raise OAuthError(INVALID_GRANT, "malformed code_verifier")
    if not hmac.compare_digest(compute_s256_challenge(verifier), challenge):
        raise OAuthError(INVALID_GRANT, "code_verifier does not match challenge")


__all__ = ["S256", "compute_s256_challenge", "verify_pkce"]
