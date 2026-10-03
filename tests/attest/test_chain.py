# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The chain must catch every way a stored record can be altered, including by
someone who can recompute digests but does not hold the signing key (ADR-143).
Real Ed25519 keys throughout; nothing here is mocked."""

from __future__ import annotations

import copy

from axiom.attest.canonical import digest
from axiom.attest.chain import (
    GENESIS,
    SIGNING_DOMAIN,
    Ed25519Signer,
    seal,
    signing_message,
    verify_chain,
)
from axiom.vega.identity.keypair import generate_keypair


def _chain(n: int = 3, signer: Ed25519Signer | None = None) -> tuple[list[dict], dict[str, bytes]]:
    signer = signer or Ed25519Signer(key_id="node-key-1", keypair=generate_keypair())
    records: list[dict] = []
    prev = GENESIS
    for i in range(1, n + 1):
        rec = seal(
            {"logbook": "operations_log", "meaning": "performed", "content": {"title": f"Round {i}"}},
            seq=i,
            prev_digest=prev,
            signer=signer,
        )
        records.append(rec)
        prev = rec["digest"]
    return records, {signer.key_id: signer.public_bytes}


def test_an_untouched_chain_verifies():
    records, keys = _chain()
    report = verify_chain(records, keys)
    assert report.ok, report
    assert report.checked == 3
    assert report.head_seq == 3
    assert report.head_digest == records[-1]["digest"]


def test_the_signature_covers_a_domain_separated_message():
    records, keys = _chain(1)
    rec = records[0]
    assert signing_message(rec["digest"]) == SIGNING_DOMAIN + bytes.fromhex(rec["digest"])
    assert rec["node_sig"]["alg"] == "ed25519"
    assert rec["node_sig"]["key_id"] == "node-key-1"


def test_editing_content_is_caught_as_a_digest_mismatch():
    records, keys = _chain()
    records[1]["content"]["title"] = "Round 2 (edited)"
    report = verify_chain(records, keys)
    assert not report.ok
    assert (report.first_bad_seq, report.reason) == (2, "digest_mismatch")


def test_recomputing_the_digest_without_the_key_is_caught_by_the_signature():
    records, keys = _chain()
    records[1]["content"]["title"] = "Round 2 (edited)"
    records[1]["digest"] = digest(records[1])
    report = verify_chain(records, keys)
    assert (report.first_bad_seq, report.reason) == (2, "bad_signature")


def test_resigning_with_another_key_under_the_same_key_id_is_caught():
    records, keys = _chain()
    forger = Ed25519Signer(key_id="node-key-1", keypair=generate_keypair())
    body = {k: v for k, v in records[1].items() if k not in ("digest", "node_sig")}
    body["content"] = {"title": "forged"}
    forged = seal(
        {k: v for k, v in body.items() if k not in ("seq", "prev_digest")},
        seq=2,
        prev_digest=records[0]["digest"],
        signer=forger,
    )
    records[1] = forged
    report = verify_chain(records, keys)
    assert (report.first_bad_seq, report.reason) == (2, "bad_signature")


def test_a_key_the_verifier_does_not_know_is_reported_as_such():
    records, _ = _chain()
    report = verify_chain(records, {})
    assert (report.first_bad_seq, report.reason) == (1, "unknown_key")


def test_deleting_a_record_is_caught():
    records, keys = _chain()
    del records[1]
    report = verify_chain(records, keys)
    assert not report.ok
    assert report.first_bad_seq == 3
    assert report.reason in ("seq_gap", "prev_mismatch")


def test_reordering_is_caught():
    records, keys = _chain()
    records[1], records[2] = records[2], records[1]
    report = verify_chain(records, keys)
    assert not report.ok
    assert report.reason == "seq_gap"


def test_a_range_verifies_from_an_anchor():
    records, keys = _chain(4)
    report = verify_chain(records[2:], keys, start_seq=3, start_prev=records[1]["digest"])
    assert report.ok
    assert report.checked == 2


def test_a_range_with_the_wrong_anchor_fails():
    records, keys = _chain(4)
    report = verify_chain(records[2:], keys, start_seq=3, start_prev=GENESIS)
    assert (report.first_bad_seq, report.reason) == (3, "prev_mismatch")


def test_seal_refuses_to_overwrite_chain_fields():
    signer = Ed25519Signer(key_id="k", keypair=generate_keypair())
    import pytest

    with pytest.raises(ValueError):
        seal({"seq": 9, "content": {}}, seq=1, prev_digest=GENESIS, signer=signer)


def test_seal_does_not_mutate_its_input():
    signer = Ed25519Signer(key_id="k", keypair=generate_keypair())
    body = {"content": {"title": "x"}}
    before = copy.deepcopy(body)
    seal(body, seq=1, prev_digest=GENESIS, signer=signer)
    assert body == before
