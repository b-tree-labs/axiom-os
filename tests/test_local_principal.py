# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""IDENT-4/8: the local principal's Ed25519 keypair + pluggable custody. The
keystone — possessing the process is no longer enough to *be* the principal."""

from __future__ import annotations

from axiom.infra.principal import attested
from axiom.vega.identity.custody import InMemoryCustody
from axiom.vega.identity.keypair import verify
from axiom.vega.identity.local import load_or_create_local_keypair


def test_local_keypair_is_created_then_stable_across_loads():
    custody = InMemoryCustody()
    kp1 = load_or_create_local_keypair(custody=custody)
    kp2 = load_or_create_local_keypair(custody=custody)   # second call loads, doesn't regen
    assert kp1.public_bytes == kp2.public_bytes            # stable identity
    assert len(kp1.public_bytes) == 32


def test_keypair_signs_and_verifies():
    kp = load_or_create_local_keypair(custody=InMemoryCustody())
    sig = kp.sign(b"capability-bytes")
    assert verify(kp.public_bytes, b"capability-bytes", sig)
    assert not verify(kp.public_bytes, b"tampered", sig)   # forgery rejected


def test_custody_is_pluggable_same_contract():
    # The same contract works for any backend (keychain/badge/hardware later).
    a, b = InMemoryCustody(), InMemoryCustody()
    kp_a = load_or_create_local_keypair(custody=a)
    kp_b = load_or_create_local_keypair(custody=b)
    assert kp_a.public_bytes != kp_b.public_bytes          # independent stores


def test_attested_principal_is_assured_and_bound_to_key():
    kp = load_or_create_local_keypair(custody=InMemoryCustody())
    p = attested(kp.public_bytes, handle="@ben:local")
    assert p.posture == "attested" and p.assured is True
    assert p.public_bytes == kp.public_bytes
    assert p.meets("attested") and not p.meets("sso")


def test_resolve_principal_follows_node_posture(monkeypatch):
    from axiom.infra.principal import resolve_principal

    monkeypatch.delenv("AXIOM_IDENTITY_POSTURE", raising=False)
    assert resolve_principal().posture == "open"          # zero-cost, no keychain

    monkeypatch.setenv("AXIOM_IDENTITY_POSTURE", "attested")
    p = resolve_principal(custody=InMemoryCustody())       # keychain-backed (mocked)
    assert p.posture == "attested" and p.assured is True and p.public_bytes is not None
