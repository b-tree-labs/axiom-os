# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Draft, present, sign, against real Postgres (ADR-142..144).

Software drafts and a person signs. Every refusal here is a way a record could
otherwise claim a human act that did not happen: the wrong person, a machine,
an administrator, a value the person never saw, or content that changed after
it was shown.
"""

from __future__ import annotations

import threading

import pytest
from sqlalchemy import text

from axiom.attest.chain import GENESIS
from axiom.extensions.builtins.attest import service
from axiom.extensions.builtins.attest.service import AttestRefused, Signatory
from axiom.infra.principal import PrincipalContext
from axiom.vega.identity.node_key import NodeKeyUnavailable

SITE = "site-a"
LOGBOOK = "demo_log"


def person(posture="sso", roles=("operator",), handle="@op1:site-a") -> Signatory:
    return Signatory(
        principal=PrincipalContext(handle=handle, posture=posture, assured=posture != "open"),
        roles=tuple(roles),
        display="Operator One",
    )


def _round_check(**content):
    fields = {"reading": "ok", "walkdown": True, **content}
    return service.create_draft(
        site_id=SITE,
        logbook=LOGBOOK,
        entry_type="ROUND_CHECK",
        meaning="performed",
        content={"title": "Round check", "fields": fields},
        origin="human",
        for_principal="@op1:site-a",
    )


def _sign(draft_id, signer, who=None, response="sign"):
    """Present, record the person's answer, and sign when the answer is sign."""
    who = who or person()
    pres = service.present(draft_id)
    service.respond(pres.presentation_id, principal=who.principal, answer=response, via="cli")
    if response != "sign":
        return service.SignResult(status=response)
    return service.sign(pres.presentation_id, signatory=who, signer=signer)


# -- the chain -----------------------------------------------------------------


def test_signing_appends_to_a_verifiable_chain(attest_db, node_signer):
    r1 = _sign(_round_check(), node_signer)
    r2 = _sign(_round_check(reading="fine"), node_signer)
    assert r1.status == "signed" and r2.status == "signed"
    assert (r1.record["seq"], r1.record["prev_digest"]) == (1, GENESIS)
    assert (r2.record["seq"], r2.record["prev_digest"]) == (2, r1.record["digest"])
    report = service.verify_logbook(SITE, LOGBOOK, {node_signer.key_id: node_signer.public_bytes})
    assert report.ok and report.checked == 2 and report.head_digest == r2.record["digest"]


def test_the_record_carries_who_signed_under_what_authority(attest_db, node_signer):
    rec = _sign(_round_check(), node_signer, who=person(roles=("operator", "admin"))).record
    assert rec["signer"]["principal"] == "@op1:site-a"
    assert rec["signer"]["kind"] == "human"
    # The admin role is held but confers nothing, so it is not recorded as authority.
    assert rec["signer"]["authority"]["roles"] == ["operator"]
    assert rec["assurance"]["posture"] == "sso"
    assert rec["meaning"] == "performed" and rec["entry_type"] == "ROUND_CHECK"
    assert rec["evidence"]["response"]["answer"] == "sign"
    assert rec["evidence"]["response"]["via"] == "cli"
    assert rec["evidence"]["presentation_digest"]
    assert [p["modality"] for p in rec["evidence"]["presented"]] == ["cli"]


def test_chains_are_separate_per_site(attest_db, node_signer):
    _sign(_round_check(), node_signer)
    other = service.create_draft(
        site_id="site-b",
        logbook=LOGBOOK,
        entry_type="ROUND_CHECK",
        meaning="performed",
        content={"title": "x", "fields": {"reading": "ok", "walkdown": True}},
        origin="human",
        for_principal="@op1:site-a",
    )
    rec = _sign(other, node_signer).record
    assert (rec["site_id"], rec["seq"], rec["prev_digest"]) == ("site-b", 1, GENESIS)


def test_concurrent_signers_get_a_gapless_chain(attest_db, node_signer):
    drafts = [_round_check(reading=str(i)) for i in range(8)]
    errors: list[BaseException] = []

    def go(d):
        try:
            _sign(d, node_signer)
        except BaseException as exc:  # noqa: BLE001 - surfaced by the assert below
            errors.append(exc)

    threads = [threading.Thread(target=go, args=(d,)) for d in drafts]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors
    records = service.records(SITE, LOGBOOK)
    assert sorted(r["seq"] for r in records) == list(range(1, 9))
    assert service.verify_logbook(SITE, LOGBOOK, {node_signer.key_id: node_signer.public_bytes}).ok


# -- the append-only guard --------------------------------------------------------


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE attest_records SET meaning = 'approved'",
        "DELETE FROM attest_records",
        "TRUNCATE attest_records",
        "UPDATE attest_presentations SET rendered = 'x'",
        "DELETE FROM attest_presentations",
    ],
)
def test_the_database_refuses_to_change_or_remove_a_record(attest_db, node_signer, statement):
    _sign(_round_check(), node_signer)
    from axiom.extensions.builtins.attest import store

    with pytest.raises(Exception, match="append-only"):
        with store.session_scope() as s:
            s.execute(text(statement))
            s.commit()


def test_an_edit_that_bypasses_the_guard_is_caught_by_verification(attest_db, node_signer):
    """The table owner can disable a trigger. The chain is what catches it."""
    _sign(_round_check(), node_signer)
    _sign(_round_check(), node_signer)
    from axiom.extensions.builtins.attest import store

    with store.session_scope() as s:
        s.execute(text("ALTER TABLE attest_records DISABLE TRIGGER attest_records_append_only"))
        s.execute(
            text(
                "UPDATE attest_records SET record = jsonb_set(record, '{meaning}', '\"approved\"') "
                "WHERE seq = 2"
            )
        )
        s.execute(text("ALTER TABLE attest_records ENABLE TRIGGER attest_records_append_only"))
        s.commit()
    report = service.verify_logbook(SITE, LOGBOOK, {node_signer.key_id: node_signer.public_bytes})
    assert not report.ok
    assert (report.first_bad_seq, report.reason) == (2, "digest_mismatch")


# -- who may sign -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("who", "reason"),
    [
        (person(posture="open"), "posture"),
        (person(posture="service"), "posture"),
        (person(posture="attested"), "posture"),  # ROUND_CHECK inherits the logbook's sso floor
        (person(roles=("visitor",)), "role"),
        (person(roles=("admin", "node_admin", "developer", "vault_custodian")), "role"),
    ],
)
def test_refuses_a_signer_without_authority(attest_db, node_signer, who, reason):
    d = _round_check()
    with pytest.raises(AttestRefused, match=reason):
        _sign(d, node_signer, who=who)
    assert service.records(SITE, LOGBOOK) == []


def test_attested_posture_meets_a_type_whose_floor_is_attested(attest_db, node_signer):
    d = service.create_draft(
        site_id=SITE,
        logbook=LOGBOOK,
        entry_type="NOTE",
        meaning="authored",
        content={"title": "note", "fields": {"text": "hello"}},
        origin="human",
        for_principal="@op1:site-a",
    )
    assert _sign(d, node_signer, who=person(posture="attested")).status == "signed"


# -- the response ---------------------------------------------------------------------


@pytest.mark.parametrize("response", ["hold", "ask"])
def test_hold_and_ask_sign_nothing_and_leave_the_draft_open(attest_db, node_signer, response):
    d = _round_check()
    result = _sign(d, node_signer, response=response)
    assert result.status == response and result.record is None
    assert service.records(SITE, LOGBOOK) == []
    assert _sign(d, node_signer).status == "signed"


def test_an_unrecognised_response_is_refused(attest_db, node_signer):
    with pytest.raises(AttestRefused, match="response"):
        _sign(_round_check(), node_signer, response="yes")


def test_a_draft_signs_once(attest_db, node_signer):
    d = _round_check()
    _sign(d, node_signer)
    with pytest.raises(AttestRefused, match="already signed"):
        _sign(d, node_signer)


def test_content_changed_after_it_was_presented_cannot_be_signed(attest_db, node_signer):
    """The person answered sign to what they saw; the content then changed.
    The answer does not carry over to content they never saw."""
    d = _round_check()
    pres = service.present(d)
    service.respond(pres.presentation_id, principal=person().principal, answer="sign", via="cli")
    service.fill(d, {"note": "added later"}, by=person())
    with pytest.raises(AttestRefused, match="changed since"):
        service.sign(pres.presentation_id, signatory=person(), signer=node_signer)
    with pytest.raises(AttestRefused, match="changed since"):
        service.respond(
            pres.presentation_id, principal=person().principal, answer="sign", via="cli"
        )


def test_signing_fails_closed_when_the_node_cannot_sign(attest_db):
    class Broken:
        key_id = "node:broken"

        def sign(self, message):
            raise NodeKeyUnavailable("no key")

    d = _round_check()
    with pytest.raises(NodeKeyUnavailable):
        _sign(d, Broken())
    assert service.records(SITE, LOGBOOK) == []
    assert service.draft(d)["status"] == "open"


# -- drafts: software proposes, it never observes -----------------------------------------


def test_drafts_are_checked_against_the_logbook(attest_db):
    with pytest.raises(AttestRefused, match="no entry type"):
        service.create_draft(
            site_id=SITE, logbook=LOGBOOK, entry_type="NOPE", meaning="performed",
            content={"title": "x", "fields": {}}, origin="human", for_principal="@op1:site-a",
        )  # fmt: skip
    with pytest.raises(AttestRefused, match="meaning"):
        service.create_draft(
            site_id=SITE, logbook=LOGBOOK, entry_type="ROUND_CHECK", meaning="approved",
            content={"title": "x", "fields": {}}, origin="human", for_principal="@op1:site-a",
        )  # fmt: skip
    with pytest.raises(AttestRefused, match="unknown field"):
        _round_check(colour="blue")
    with pytest.raises(AttestRefused, match="no logbook"):
        service.create_draft(
            site_id=SITE, logbook="missing", entry_type="X", meaning="performed",
            content={"title": "x", "fields": {}}, origin="human", for_principal="@op1:site-a",
        )  # fmt: skip


def test_software_may_not_supply_an_observed_value(attest_db):
    with pytest.raises(AttestRefused, match="observe"):
        service.create_draft(
            site_id=SITE, logbook=LOGBOOK, entry_type="ROUND_CHECK", meaning="performed",
            content={"title": "x", "fields": {"reading": "ok"}},
            origin="agent:helper", for_principal="@op1:site-a",
        )  # fmt: skip


def test_a_software_draft_becomes_signable_once_the_person_enters_what_they_observed(
    attest_db, node_signer
):
    d = service.create_draft(
        site_id=SITE, logbook=LOGBOOK, entry_type="ROUND_CHECK", meaning="performed",
        content={"title": "Round check", "fields": {"walkdown": True}},
        origin="agent:helper", for_principal="@op1:site-a",
    )  # fmt: skip
    with pytest.raises(AttestRefused, match="required"):
        service.present(d)
    service.fill(d, {"reading": "ok"}, by=person())
    rec = _sign(d, node_signer).record
    assert rec["origin"] == "agent:helper"
    assert rec["evidence"]["provenance"] == {"reading": "@op1:site-a", "walkdown": "agent:helper"}


def test_only_the_person_the_draft_is_for_can_fill_it(attest_db):
    d = _round_check()
    with pytest.raises(AttestRefused, match="for"):
        service.fill(d, {"note": "x"}, by=person(handle="@someone:site-a"))


def test_only_the_person_the_draft_is_for_can_sign_it(attest_db, node_signer):
    pres = service.present(_round_check())
    other = person(handle="@op2:site-a")
    with pytest.raises(AttestRefused, match="is for"):
        service.respond(pres.presentation_id, principal=other.principal, answer="sign", via="cli")
    # Even after the right person answers, someone else cannot sign on it.
    service.respond(pres.presentation_id, principal=person().principal, answer="sign", via="cli")
    with pytest.raises(AttestRefused, match="is for"):
        service.sign(pres.presentation_id, signatory=other, signer=node_signer)
