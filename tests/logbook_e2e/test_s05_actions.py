# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""S5 Supplement, acknowledge, co-sign (#1818): each action is its own signed record on the one
chain, under its target, and none changes the target."""

from __future__ import annotations

import pytest

from .conftest import ANN, BO, DEE, LOG, SITE, check, open_session, sign_cli, sign_web, signatory


def _sign_action(p, draft_id, *, web: bool = False, minutes_ago: float = 1):
    from axiom.extensions.builtins.attest import service, signing

    if web:
        return sign_web(p, draft_id, minutes_ago=minutes_ago)
    pres = service.present(draft_id)
    service.respond(
        pres.presentation_id, principal=signatory(p).principal, answer="sign", via="cli"
    )
    return service.sign(
        pres.presentation_id, signatory=signatory(p), signer=signing.signer()
    ).record


@pytest.mark.acceptance("LB-E2E-A10")
def test_lb_e2e_a10_a_supplement_leaves_the_original_and_meets_no_obligation(world):
    from axiom.attest.canonical import digest
    from axiom.extensions.builtins.attest import obligations, service

    open_session(world)
    original = check(world)
    before = service.record(original["attestation_id"])
    met_before = obligations.evaluate(SITE, LOG, now=world.clock["t"])[0]["last_met_at"]
    d = service.create_supplement_draft(site_id=SITE, target=original["attestation_id"], reason="correction",
                                        content={"fields": {"readings": {"flow": "12.4"}}},
                                        origin="human", for_principal=ANN.handle)  # fmt: skip
    sup = sign_web(ANN, d, device="desk-1")
    after = service.record(original["attestation_id"])
    assert after == before and digest(after) == before["digest"]
    assert [r["attestation_id"] for r in service.thread(original["attestation_id"])] == [
        original["attestation_id"],
        sup["attestation_id"],
    ]
    assert sup["kind"] == "supplement" and sup["target"] == original["attestation_id"]
    assert obligations.evaluate(SITE, LOG, now=world.clock["t"])[0]["last_met_at"] == met_before
    assert sup.get("interval") is None or sup["interval"].get("event") is None


@pytest.mark.acceptance("LB-E2E-A11")
def test_lb_e2e_a11_one_acknowledgement_by_someone_else(world):
    from axiom.extensions.builtins.attest import service

    note = sign_cli(ANN, "NOTE", {"text": "for the lead"})
    assert [r["attestation_id"] for r in service.unacknowledged(SITE, LOG)] == [
        note["attestation_id"]
    ]
    with pytest.raises(service.AttestRefused):
        service.create_ack_draft(site_id=SITE, target=note["attestation_id"], origin="human",
                                 for_principal=ANN.handle)  # fmt: skip
    ack = _sign_action(BO, service.create_ack_draft(site_id=SITE, target=note["attestation_id"],
                                                    origin="human", for_principal=BO.handle))  # fmt: skip
    assert ack["kind"] == "ack" and ack["target"] == note["attestation_id"]
    assert service.unacknowledged(SITE, LOG) == []
    with pytest.raises(service.AttestRefused):
        service.create_ack_draft(site_id=SITE, target=note["attestation_id"], origin="human",
                                 for_principal=BO.handle)  # fmt: skip


@pytest.mark.acceptance("LB-E2E-A12")
def test_lb_e2e_a12_a_change_is_co_signed_once_by_a_fresh_lead(world):
    from axiom.extensions.builtins.attest import grants, service

    d = service.create_draft(site_id=SITE, logbook=LOG, entry_type="CHANGE", meaning="performed",
                             content={"title": "Change", "fields": {"text": "the desk setting moved"}},
                             origin="human", for_principal=ANN.handle)  # fmt: skip
    change = sign_web(ANN, d, device="desk-1")
    pending = service.pending_cosigns(SITE, LOG)
    assert [p["attestation_id"] for p in pending] == [change["attestation_id"]]
    assert [r for r, _ in world.cosign_sent] == [BO.handle], "the lead is told once"
    for who in (ANN, DEE):
        with pytest.raises(service.AttestRefused):
            _sign_action(who, service.create_cosign_draft(site_id=SITE, target=change["attestation_id"],
                                                          origin="human", for_principal=who.handle))  # fmt: skip
    co = service.create_cosign_draft(site_id=SITE, target=change["attestation_id"], origin="human",
                                     for_principal=BO.handle)  # fmt: skip
    with pytest.raises(grants.GrantRefused) as stale:
        _sign_action(BO, co, web=True, minutes_ago=6)
    assert stale.value.code == "reauth_required"
    signed = _sign_action(BO, co, web=True, minutes_ago=1)
    assert signed["kind"] == "cosign" and signed["assurance"]["fresh_within_met"] is True
    assert service.pending_cosigns(SITE, LOG) == []
    assert service.briefs([service.record(change["attestation_id"])])[0]["cosign"] == "done"


@pytest.mark.acceptance("LB-E2E-A13")
def test_lb_e2e_a13_only_the_relieved_party_co_signs_a_handover(world):
    # #1807: ``cosign.party`` names the entry's field holding the one person who may co-sign.
    from axiom.extensions.builtins.attest import roles, service

    from .conftest import Person

    eve = Person("eve", SITE, ("lead",), "Eve Example")  # a lead who is not the relieved party
    roles.grant(eve.handle, SITE, "lead", by="@admin:example-site", state_dir=world.state_dir)
    d = service.create_draft(site_id=SITE, logbook=LOG, entry_type="HANDOVER", meaning="performed",
                             content={"title": "Handover", "fields": {"text": "to Bo",
                                                                      "relieved": BO.handle}},
                             origin="human", for_principal=ANN.handle)  # fmt: skip
    h = sign_web(ANN, d, device="desk-1")
    with pytest.raises(service.AttestRefused, match="relieved|party"):
        _sign_action(eve, service.create_cosign_draft(site_id=SITE, target=h["attestation_id"],
                                                      origin="human", for_principal=eve.handle))  # fmt: skip
    co = _sign_action(BO, service.create_cosign_draft(site_id=SITE, target=h["attestation_id"],
                                                      origin="human", for_principal=BO.handle))  # fmt: skip
    assert co["kind"] == "cosign" and co["signer"]["principal"] == BO.handle


@pytest.mark.acceptance("LB-E2E-A12")
def test_lb_e2e_a12_a_site_narrows_who_co_signs_and_never_widens_it(world):
    # #1815: ``cosign.roles = { site_key, default }``; the site's value is intersected with the
    # declared roles, so it may narrow them and never add one; the effective rule is reported.
    import logging

    from axiom.extensions.builtins.attest import service, site_settings
    from axiom.extensions.builtins.attest.skills import bind_default
    from axiom.infra.skills import SkillContext

    def describe():
        ctx = SkillContext(registry=bind_default(), state_dir=world.state_dir,
                           logger=logging.getLogger("e2e"))  # fmt: skip
        r = ctx.registry.invoke("attest.logbook_list", {"logbook": LOG, "site": SITE}, ctx)
        (book,) = r.value["logbooks"]
        return {t["id"]: t for t in book["types"]}["CHANGE"]["cosign"]

    assert describe()["effective_roles"] == ["lead", "member"]
    site_settings.register(lambda site: {"desk": {"cosign": {"change_roles": ["lead", "admin", "x"]}}})
    try:
        d = service.create_draft(site_id=SITE, logbook=LOG, entry_type="CHANGE", meaning="performed",
                                 content={"title": "Change", "fields": {"text": "moved"}},
                                 origin="human", for_principal=ANN.handle)  # fmt: skip
        change = sign_web(ANN, d, device="desk-1")
        (pending,) = service.pending_cosigns(SITE, LOG)
        assert pending["cosign_roles"] == ["lead"]
        co = service.create_cosign_draft(site_id=SITE, target=change["attestation_id"],
                                         origin="human", for_principal=BO.handle)  # fmt: skip
        assert service.rules_for(co).roles == ("lead",)
        got = describe()
        assert got["roles"] == ["lead", "member"] and got["effective_roles"] == ["lead"]
        assert got["site_key"] == "desk.cosign.change_roles"
    finally:
        site_settings.unregister()
