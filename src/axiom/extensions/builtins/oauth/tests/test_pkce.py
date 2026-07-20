# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""PKCE S256 verification (RFC 7636) — the possession proof for public clients."""

from __future__ import annotations

import base64
import hashlib

import pytest

from axiom.extensions.builtins.oauth.errors import OAuthError
from axiom.extensions.builtins.oauth.pkce import (
    S256,
    compute_s256_challenge,
    verify_pkce,
)


def _challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def test_matching_s256_verifier_passes():
    verifier = "a" * 64
    verify_pkce(verifier, _challenge(verifier), S256)  # must not raise


def test_compute_matches_reference():
    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    assert compute_s256_challenge(verifier) == _challenge(verifier)


def test_wrong_verifier_is_invalid_grant():
    verifier = "a" * 64
    with pytest.raises(OAuthError) as exc:
        verify_pkce("b" * 64, _challenge(verifier), S256)
    assert exc.value.error == "invalid_grant"


def test_missing_verifier_is_invalid_grant():
    with pytest.raises(OAuthError) as exc:
        verify_pkce(None, _challenge("a" * 64), S256)
    assert exc.value.error == "invalid_grant"


def test_plain_method_rejected_oauth21_requires_s256():
    # OAuth 2.1 forbids the 'plain' method; only S256 is accepted.
    verifier = "a" * 64
    with pytest.raises(OAuthError) as exc:
        verify_pkce(verifier, verifier, "plain")
    assert exc.value.error == "invalid_request"


def test_too_short_verifier_rejected():
    with pytest.raises(OAuthError) as exc:
        verify_pkce("short", _challenge("short"), S256)
    assert exc.value.error == "invalid_grant"
