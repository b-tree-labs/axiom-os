# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for webauth JWT create/verify (PyJWT)."""

from __future__ import annotations

from datetime import timedelta

from axiom.webauth import create_access_token, create_refresh_token, verify_token

SECRET = "k" * 40  # >= 32 bytes: avoids PyJWT's short-key warning


def test_access_token_roundtrip() -> None:
    tok = create_access_token({"sub": "@u:webapp"}, secret_key=SECRET)
    payload = verify_token(tok, secret_key=SECRET)
    assert payload is not None
    assert payload["sub"] == "@u:webapp"
    assert payload["type"] == "access"


def test_refresh_token_marked() -> None:
    tok = create_refresh_token({"sub": "@u:webapp"}, secret_key=SECRET)
    assert verify_token(tok, secret_key=SECRET)["type"] == "refresh"


def test_tampered_and_wrong_secret_rejected() -> None:
    tok = create_access_token({"sub": "@u:webapp"}, secret_key=SECRET)
    assert verify_token(tok + "x", secret_key=SECRET) is None
    assert verify_token(tok, secret_key="z" * 40) is None


def test_expired_token_rejected() -> None:
    tok = create_access_token(
        {"sub": "@u:webapp"}, expires_delta=timedelta(seconds=-1), secret_key=SECRET
    )
    assert verify_token(tok, secret_key=SECRET) is None
