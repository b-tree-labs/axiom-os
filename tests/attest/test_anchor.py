# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Anchors: a signed Merkle root over every logbook head at a site (ADR-143).

A chain alone cannot catch a forger who holds the node key: they can rewrite a
record and re-sign everything after it. An anchor printed or sent elsewhere
fixes each head at a moment, so that rewrite no longer matches it."""

from __future__ import annotations

import pytest

from axiom.attest.anchor import (
    ANCHOR_DOMAIN,
    anchor_message,
    merkle_root,
    sign_anchor,
    verify_anchor,
)
from axiom.attest.chain import SIGNING_DOMAIN, Ed25519Signer
from axiom.vega.identity.keypair import generate_keypair

A = {"logbook": "a_log", "seq": 3, "digest": "aa" * 32}
B = {"logbook": "b_log", "seq": 9, "digest": "bb" * 32}
C = {"logbook": "c_log", "seq": 1, "digest": "cc" * 32}


def test_root_does_not_depend_on_the_order_heads_are_given():
    assert merkle_root([A, B, C]) == merkle_root([C, A, B])


def test_every_head_field_is_committed():
    base = merkle_root([A, B, C])
    assert merkle_root([{**A, "seq": 4}, B, C]) != base
    assert merkle_root([{**A, "digest": "ab" * 32}, B, C]) != base
    assert merkle_root([{**A, "logbook": "z_log"}, B, C]) != base


def test_one_head_and_an_odd_count_have_roots():
    assert len(merkle_root([A])) == 64
    assert merkle_root([A, B, C]) != merkle_root([A, B])


def test_the_construction_is_pinned_for_independent_verifiers():
    """Leaves hash under 0x00 and inner nodes under 0x01 (RFC 6962 style); an
    odd node is promoted, not paired with itself. A verifier in another
    language reproduces exactly this."""
    import hashlib

    from axiom.attest.canonical import canonical_bytes

    def leaf(h):
        return hashlib.sha256(b"\x00" + canonical_bytes(h)).digest()

    def node(left, right):
        return hashlib.sha256(b"\x01" + left + right).digest()

    assert merkle_root([A]) == leaf(A).hex()
    assert merkle_root([A, B]) == node(leaf(A), leaf(B)).hex()
    assert merkle_root([A, B, C]) == node(node(leaf(A), leaf(B)), leaf(C)).hex()


def test_no_heads_is_refused():
    with pytest.raises(ValueError):
        merkle_root([])


def test_duplicate_logbooks_are_refused():
    with pytest.raises(ValueError, match="twice"):
        merkle_root([A, {**A, "seq": 9}])


def test_signed_anchor_verifies_and_is_domain_separated():
    signer = Ed25519Signer("node:t", generate_keypair())
    root = merkle_root([A, B])
    sig = sign_anchor(root, signer)
    keys = {"node:t": signer.public_bytes}
    assert verify_anchor(root, [A, B], sig, keys) == (True, None)
    assert anchor_message(root).startswith(ANCHOR_DOMAIN)
    assert ANCHOR_DOMAIN != SIGNING_DOMAIN


@pytest.mark.parametrize(
    ("heads", "reason"),
    [
        ([A, {**B, "digest": "bc" * 32}], "root_mismatch"),
        ([A], "root_mismatch"),
    ],
)
def test_anchor_verification_names_what_is_wrong(heads, reason):
    signer = Ed25519Signer("node:t", generate_keypair())
    root = merkle_root([A, B])
    sig = sign_anchor(root, signer)
    assert verify_anchor(root, heads, sig, {"node:t": signer.public_bytes}) == (False, reason)


def test_anchor_signed_by_another_key_fails():
    signer = Ed25519Signer("node:t", generate_keypair())
    other = Ed25519Signer("node:t", generate_keypair())
    root = merkle_root([A])
    assert verify_anchor(root, [A], sign_anchor(root, other), {"node:t": signer.public_bytes}) == (
        False,
        "bad_signature",
    )
