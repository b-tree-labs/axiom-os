# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Co-signatures (unit L4): the declaration, the rules row, the card, the thread and the skill
declarations. The acceptance rows live in ``tests/logbook_acceptance/test_l4.py``."""

from __future__ import annotations

from pathlib import Path

import pytest

from axiom.extensions.builtins.attest import cosign, registry, service
from axiom.extensions.builtins.attest.logbooks import parse_logbook
from axiom.extensions.builtins.attest.skills import SPECS

from .test_signing import SITE, person

PAIR_LOG = (
    Path(__file__).resolve().parents[6]
    / "tests"
    / "logbook_acceptance"
    / "logbooks"
    / "pair_log.toml"
)
MEMBER = "@op1:site-a"
LEAD = "@op2:site-a"


@pytest.fixture
def pair_log(attest_db):
    registry.register_file(PAIR_LOG)
    cosign.set_sender(lambda recipient, info: None)
    yield registry.get("pair_log")
    cosign.reset_sender()


def _sign(draft_id, signer, who):
    p = service.present(draft_id)
    service.respond(p.presentation_id, principal=who.principal, answer="sign", via="cli")
    return service.sign(p.presentation_id, signatory=who, signer=signer).record


def _check(signer):
    d = service.create_draft(
        site_id=SITE, logbook="pair_log", entry_type="CHECK", meaning="performed",
        content={"title": "Check", "fields": {"ok": True}}, origin="human", for_principal=MEMBER,
    )  # fmt: skip
    return _sign(d, signer, person(handle=MEMBER, roles=("member",)))


def test_the_cosign_declaration_defaults_to_the_types_assurance():
    b = parse_logbook({
        "logbook": {"id": "x_log", "version": "1", "assurance": {"posture": "attested"}},
        "type": [
            {"id": "A", "meanings": ["authored"], "roles": ["r"], "cosign": {"roles": ["s"]}},
            {"id": "B", "meanings": ["authored"], "roles": ["r"], "assurance": {"posture": "sso", "fresh_within": "2m"},
             "cosign": {"roles": ["s"]}},
            {"id": "C", "meanings": ["authored"], "roles": ["r"], "cosign": {"roles": ["s"], "assurance": {"fresh_within": "1h"}}},
        ],
    })  # fmt: skip
    assert (b.type("A").cosign.posture, b.type("A").cosign.fresh_within_seconds) == (
        "attested",
        None,
    )
    assert (b.type("B").cosign.posture, b.type("B").cosign.fresh_within_seconds) == ("sso", 120)
    assert (b.type("C").cosign.posture, b.type("C").cosign.fresh_within_seconds) == (
        "attested",
        3600,
    )
    assert b.not_yet_enforced == ()


def test_the_cosign_rules_row(pair_log, node_signer):
    rec = _check(node_signer)
    rules = service.rules_for(
        service.create_cosign_draft(
            site_id=SITE, target=rec["attestation_id"], origin="human", for_principal=LEAD
        )
    )
    assert (
        rules.roles == ("lead",) and rules.posture == "sso" and rules.fresh_within_seconds is None
    )
    assert "phone" not in rules.sign_devices and rules.presence is None
    assert rules.what == "pair_log.CHECK (co-sign)"


def test_the_card_shows_what_is_countersigned(pair_log, node_signer):
    rec = _check(node_signer)
    d = service.create_cosign_draft(
        site_id=SITE, target=rec["attestation_id"], origin="human", for_principal=LEAD
    )
    draft = service.draft(d)
    assert (draft["kind"], draft["target"], draft["meaning"]) == (
        "cosign",
        rec["attestation_id"],
        "approved",
    )
    p = service.present(d)
    assert p.rendered.splitlines() == [
        f"Co-sign #{rec['seq']} ({rec['digest'][:8]})",
        "  Original: CHECK · Check",
        "  All in order: yes",
    ]
    assert p.presenter.endswith(".cosign@1") and "Co-sign entry" in p.speakable
    c = _sign(d, node_signer, person(handle=LEAD, roles=("lead",)))
    thread = service.thread(rec["attestation_id"])
    assert [(t["kind"], t["cosign"]) for t in thread] == [("statement", "done"), ("cosign", None)]
    assert c["target_digest"] == rec["digest"]


def test_recipients_hold_the_role_and_are_not_the_signer(tmp_path, monkeypatch):
    from axiom.extensions.builtins.attest import roles

    monkeypatch.setattr(cosign, "_state_dir", lambda: tmp_path)
    roles.grant("@a:s", "s", "lead", by="@admin:s", state_dir=tmp_path)
    roles.grant("@b:s", "s", "lead", by="@admin:s", state_dir=tmp_path)
    roles.grant("@c:s", "s", "member", by="@admin:s", state_dir=tmp_path)
    assert cosign.recipients("s", ("lead",), except_principal="@a:s") == ["@b:s"]
    # An administration role in the wanted set names nobody (the role store refuses to grant one).
    assert cosign.recipients("s", ("lead", "admin"), except_principal="@x:s") == ["@a:s", "@b:s"]
    assert cosign.recipients("s", ("admin",), except_principal="@x:s") == []
    assert cosign.recipients("other", ("lead",), except_principal="@x:s") == []


def test_cosign_skills_declare_risk_and_requires():
    by_name = {s.name: s for s in SPECS}
    assert by_name["attest.cosign"].risk == "reversible-write" and by_name["attest.cosign"].requires
    assert by_name["attest.cosign"].surfaces == ("cli",)
    assert by_name["attest.cosign_pending"].risk == "read"
    assert by_name["attest.cosign_pending"].side_effects is False
