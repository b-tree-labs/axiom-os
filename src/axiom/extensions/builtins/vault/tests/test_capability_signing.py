# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""IDENT-5/6: real Ed25519 capability signing + verification, retiring the
b'\\x00'*64 placeholder. A forged/tampered capability fails verification."""

from __future__ import annotations

from axiom.extensions.builtins.vault.capability_store import (
    VaultContext,
    issue_capability,
    verify_capability,
)
from axiom.governance import Classification, IntentPattern, ResourcePattern
from axiom.vega.identity.keypair import generate_keypair
from axiom.vega.identity.principal import Principal


def _issue(ctx):
    return issue_capability(
        ctx,
        subject=Principal(handle="@alice:test", public_bytes=b"\x00" * 32),
        intent_pattern=IntentPattern("notification.send"),
        resource_pattern=ResourcePattern("slack://*"),
        classification_ceiling=Classification.INTERNAL,
    )


def test_issued_capability_has_a_real_signature_and_verifies():
    ctx = VaultContext(signer=generate_keypair())
    cap = _issue(ctx)
    assert cap.signature != b"\x00" * 64                 # placeholder retired
    assert ctx.issuer.public_bytes != b"\x00" * 32       # real issuer key
    assert verify_capability(ctx, cap) is True


def test_tampered_capability_fails_verification():
    ctx = VaultContext(signer=generate_keypair())
    cap = _issue(ctx)
    # Forge: same signature, different scope -> canonical bytes change -> fail.
    import dataclasses

    forged = dataclasses.replace(cap, resource_pattern=ResourcePattern("box://*"))
    assert verify_capability(ctx, forged) is False


def test_unsigned_placeholder_fails_verification():
    ctx = VaultContext(signer=generate_keypair())
    cap = _issue(ctx)
    import dataclasses

    placeholder = dataclasses.replace(cap, signature=b"\x00" * 64)
    assert verify_capability(ctx, placeholder) is False
