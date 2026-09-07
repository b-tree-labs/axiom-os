# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Federation Digest — signed envelope of findings offered peer-to-peer."""

from __future__ import annotations


def test_build_digest_signs_envelope() -> None:
    from axiom.findings import mint
    from axiom.vega.federation import build_digest, verify_digest
    from axiom.vega.identity import generate_keypair

    author = generate_keypair()
    node = generate_keypair()

    f = mint(claim="c", evidence=[], author_handle="@a", author_keypair=author)

    digest = build_digest(
        findings=[f],
        from_node="@example-host:example-org",
        node_keypair=node,
        to_node="@laptop:axiom",
    )
    assert digest.from_node == "@example-host:example-org"
    assert digest.to_node == "@laptop:axiom"
    assert len(digest.findings) == 1
    assert digest.node_signature

    pubkeys = {
        "@a": author.public_bytes,
        "@example-host:example-org": node.public_bytes,
    }
    assert verify_digest(digest, pubkeys) is True


def test_verify_digest_rejects_bad_node_signature() -> None:
    from axiom.findings import mint
    from axiom.vega.federation import build_digest, verify_digest
    from axiom.vega.identity import generate_keypair

    author = generate_keypair()
    node = generate_keypair()
    impostor = generate_keypair()

    f = mint(claim="c", evidence=[], author_handle="@a", author_keypair=author)
    digest = build_digest(
        findings=[f],
        from_node="@example-host:example-org",
        node_keypair=node,
        to_node="@laptop:axiom",
    )
    pubkeys = {
        "@a": author.public_bytes,
        "@example-host:example-org": impostor.public_bytes,  # wrong key
    }
    assert verify_digest(digest, pubkeys) is False


def test_verify_digest_rejects_tampered_finding() -> None:
    from dataclasses import replace

    from axiom.findings import mint
    from axiom.vega.federation import build_digest, verify_digest
    from axiom.vega.identity import generate_keypair

    author = generate_keypair()
    node = generate_keypair()
    f = mint(claim="good", evidence=[], author_handle="@a", author_keypair=author)
    digest = build_digest(
        findings=[f],
        from_node="@example-host",
        node_keypair=node,
        to_node="@peer",
    )
    # Tamper with the finding inside the digest.
    bad = replace(digest, findings=(f.with_claim("bad"),))
    pubkeys = {"@a": author.public_bytes, "@example-host": node.public_bytes}
    assert verify_digest(bad, pubkeys) is False
