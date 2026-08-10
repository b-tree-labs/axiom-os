# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Content-addressed, chain-signed findings (ADR-021 P2).

Each finding has a content hash and a chain of signatures (contributor +
verifiers + eval attestations). Rebroadcast preserves the chain.
"""

from __future__ import annotations


def test_mint_finding_produces_content_hash_and_signature() -> None:
    from axiom.findings import Finding, mint
    from axiom.vega.identity import generate_keypair

    author = generate_keypair()
    f = mint(
        claim="Xe-135 cross-section peaks at thermal energies",
        evidence=["doc:neutron-poisoning-guide", "doc:barn-tables"],
        author_handle="@ben.booth:axiom",
        author_keypair=author,
    )
    assert isinstance(f, Finding)
    assert f.content_hash  # 64 hex chars
    assert len(f.content_hash) == 64
    assert len(f.signatures) == 1
    assert f.signatures[0].signer == "@ben.booth:axiom"


def test_verify_finding_checks_all_signatures() -> None:
    from axiom.findings import mint, verify_finding
    from axiom.vega.identity import generate_keypair

    author = generate_keypair()
    f = mint(
        claim="x",
        evidence=[],
        author_handle="@a:axiom",
        author_keypair=author,
    )
    pubkeys = {"@a:axiom": author.public_bytes}
    assert verify_finding(f, pubkeys) is True


def test_verify_rejects_tampered_claim() -> None:
    from axiom.findings import mint, verify_finding
    from axiom.vega.identity import generate_keypair

    author = generate_keypair()
    f = mint(
        claim="original claim", evidence=[], author_handle="@a", author_keypair=author
    )
    tampered = f.with_claim("changed claim")
    assert verify_finding(tampered, {"@a": author.public_bytes}) is False


def test_attest_adds_to_signature_chain() -> None:
    from axiom.findings import attest, mint, verify_finding
    from axiom.vega.identity import generate_keypair

    author = generate_keypair()
    verifier = generate_keypair()

    f = mint(
        claim="c", evidence=[], author_handle="@a", author_keypair=author
    )
    f2 = attest(f, attestor_handle="@v", attestor_keypair=verifier, role="eval-gate")

    assert len(f2.signatures) == 2
    assert f2.signatures[1].signer == "@v"
    assert f2.signatures[1].role == "eval-gate"

    # Verification must pass with both keys present.
    assert verify_finding(
        f2, {"@a": author.public_bytes, "@v": verifier.public_bytes}
    ) is True


def test_verify_rejects_unknown_signer() -> None:
    from axiom.findings import mint, verify_finding
    from axiom.vega.identity import generate_keypair

    author = generate_keypair()
    f = mint(claim="c", evidence=[], author_handle="@a", author_keypair=author)
    # Empty pubkey registry — can't verify the author.
    assert verify_finding(f, {}) is False


def test_content_hash_is_stable_across_mints_of_same_content() -> None:
    from axiom.findings import mint
    from axiom.vega.identity import generate_keypair

    k1 = generate_keypair()
    k2 = generate_keypair()
    f1 = mint(claim="c", evidence=["e1"], author_handle="@a", author_keypair=k1)
    f2 = mint(claim="c", evidence=["e1"], author_handle="@a", author_keypair=k2)
    # Different signatures (different keys) but same content hash.
    assert f1.content_hash == f2.content_hash
    assert f1.signatures[0].signature != f2.signatures[0].signature
