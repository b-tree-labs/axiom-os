# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Site anchors against real Postgres (ADR-143, Anchors)."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from axiom.attest.chain import seal
from axiom.extensions.builtins.attest import service, store
from axiom.extensions.builtins.attest.service import AttestRefused

from .test_signing import LOGBOOK, SITE, _round_check, _sign


def _keys(signer):
    return {signer.key_id: signer.public_bytes}


def test_anchor_covers_every_logbook_head_at_the_site(attest_db, node_signer):
    _sign(_round_check(), node_signer)
    last = _sign(_round_check(), node_signer).record
    a = service.anchor_site(SITE, node_signer)
    assert a["heads"] == [{"logbook": LOGBOOK, "seq": 2, "digest": last["digest"]}]
    assert service.verify_site_anchor(a["anchor_id"], _keys(node_signer)) == (True, None)


def test_an_anchor_needs_something_to_anchor(attest_db, node_signer):
    with pytest.raises(AttestRefused, match="no chains"):
        service.anchor_site(SITE, node_signer)


def test_anchors_are_append_only(attest_db, node_signer):
    _sign(_round_check(), node_signer)
    service.anchor_site(SITE, node_signer)
    with pytest.raises(Exception, match="append-only"):
        with store.session_scope() as s:
            s.execute(text("UPDATE attest_anchors SET root = 'x'"))
            s.commit()


def test_a_rewrite_signed_with_the_node_key_passes_the_chain_but_not_the_anchor(
    attest_db, node_signer
):
    """The attack an anchor exists for: whoever holds the node key rewrites a
    record and re-signs it. The chain verifies; the earlier anchor does not."""
    _sign(_round_check(), node_signer)
    original = _sign(_round_check(), node_signer).record
    anchor = service.anchor_site(SITE, node_signer)

    forged_body = {
        k: v for k, v in original.items() if k not in ("seq", "prev_digest", "digest", "node_sig")
    }
    forged_body["meaning"] = "performed"
    forged_body["content"] = {**original["content"], "title": "Round check (rewritten)"}
    forged = seal(forged_body, seq=2, prev_digest=original["prev_digest"], signer=node_signer)
    with store.session_scope() as s:
        s.execute(text("ALTER TABLE attest_records DISABLE TRIGGER attest_records_append_only"))
        s.execute(
            text(
                "UPDATE attest_records SET record = CAST(:rec AS jsonb), digest = :d "
                "WHERE site_id = :site AND logbook = :logbook AND seq = 2"
            ),
            {
                "rec": __import__("json").dumps(forged),
                "d": forged["digest"],
                "site": SITE,
                "logbook": LOGBOOK,
            },
        )
        s.execute(text("ALTER TABLE attest_records ENABLE TRIGGER attest_records_append_only"))
        s.execute(
            text(
                "UPDATE attest_chain_heads SET digest = :d WHERE site_id = :site AND logbook = :logbook"
            ),
            {"d": forged["digest"], "site": SITE, "logbook": LOGBOOK},
        )
        s.commit()

    assert service.verify_logbook(SITE, LOGBOOK, _keys(node_signer)).ok, (
        "the forgery re-signed the chain"
    )
    ok, reason = service.verify_site_anchor(anchor["anchor_id"], _keys(node_signer))
    assert not ok and reason == f"head_rewritten:{LOGBOOK}"
