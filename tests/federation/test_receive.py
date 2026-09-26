# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Receive pipeline: verify digest → eval gate → route to quarantine or corpus.

ADR-021 P3: a federated finding does not enter local corpus until it
passes local evals. Quarantine is the first stop for new peers (P6).
"""

from __future__ import annotations


def test_receive_rejects_invalid_digest() -> None:
    from axiom.findings import mint
    from axiom.vega.federation import build_digest, receive_digest
    from axiom.vega.identity import generate_keypair

    author = generate_keypair()
    node = generate_keypair()
    f = mint(claim="c", evidence=[], author_handle="@a", author_keypair=author)
    digest = build_digest(findings=[f], from_node="@example-host", node_keypair=node, to_node="@peer")
    # Pubkey registry doesn't know the node — verification fails.
    outcome = receive_digest(digest, pubkeys={}, peer_status="cluster", eval_fn=lambda f: 1.0)
    assert outcome.accepted == []
    assert outcome.rejected[0].reason == "invalid_signature"


def test_receive_routes_quarantined_peer_findings_to_quarantine() -> None:
    from axiom.findings import mint
    from axiom.vega.federation import build_digest, receive_digest
    from axiom.vega.identity import generate_keypair

    author = generate_keypair()
    node = generate_keypair()
    f = mint(claim="c", evidence=[], author_handle="@a", author_keypair=author)
    digest = build_digest(findings=[f], from_node="@example-host", node_keypair=node, to_node="@peer")
    pubkeys = {"@a": author.public_bytes, "@example-host": node.public_bytes}

    outcome = receive_digest(
        digest, pubkeys=pubkeys, peer_status="quarantine", eval_fn=lambda f: 1.0
    )
    assert outcome.accepted == []
    assert outcome.quarantined == [f]


def test_receive_rejects_findings_that_fail_eval_gate() -> None:
    from axiom.findings import mint
    from axiom.vega.federation import build_digest, receive_digest
    from axiom.vega.identity import generate_keypair

    author = generate_keypair()
    node = generate_keypair()
    f = mint(claim="c", evidence=[], author_handle="@a", author_keypair=author)
    digest = build_digest(findings=[f], from_node="@example-host", node_keypair=node, to_node="@peer")
    pubkeys = {"@a": author.public_bytes, "@example-host": node.public_bytes}

    outcome = receive_digest(digest, pubkeys=pubkeys, peer_status="cluster", eval_fn=lambda f: 0.2)
    assert outcome.accepted == []
    assert outcome.rejected[0].reason == "eval_gate_failed"


def test_receive_accepts_trusted_peer_findings_that_pass_eval() -> None:
    from axiom.findings import mint
    from axiom.vega.federation import build_digest, receive_digest
    from axiom.vega.identity import generate_keypair

    author = generate_keypair()
    node = generate_keypair()
    f = mint(claim="c", evidence=[], author_handle="@a", author_keypair=author)
    digest = build_digest(findings=[f], from_node="@example-host", node_keypair=node, to_node="@peer")
    pubkeys = {"@a": author.public_bytes, "@example-host": node.public_bytes}

    outcome = receive_digest(digest, pubkeys=pubkeys, peer_status="cluster", eval_fn=lambda f: 0.95)
    assert outcome.accepted == [f]
    assert outcome.quarantined == []
    assert outcome.rejected == []


def test_receive_updates_peer_reputation_stats() -> None:
    """ADR-021 P7: local reputation = pass rate, per peer, per node."""
    from axiom.findings import mint
    from axiom.vega.federation import build_digest, receive_digest
    from axiom.vega.identity import generate_keypair

    author = generate_keypair()
    node = generate_keypair()
    pubkeys = {"@a": author.public_bytes, "@example-host": node.public_bytes}

    def make_digest(claim):
        f = mint(claim=claim, evidence=[], author_handle="@a", author_keypair=author)
        return build_digest(findings=[f], from_node="@example-host", node_keypair=node, to_node="@peer")

    # First one passes, second fails eval.
    good = make_digest("good")
    bad = make_digest("bad")

    o1 = receive_digest(good, pubkeys=pubkeys, peer_status="cluster", eval_fn=lambda f: 1.0)
    o2 = receive_digest(bad, pubkeys=pubkeys, peer_status="cluster", eval_fn=lambda f: 0.1)

    assert o1.peer_pass_rate == 1.0  # 1 of 1
    # Receive is stateless — the caller aggregates. We check per-call stats.
    assert o2.peer_pass_rate == 0.0
