# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Acknowledgements and the per-kind signing rules (unit L3): the rules each kind signs under,
the card, and the skills. The acceptance rows live in ``tests/logbook_acceptance/test_l3.py``."""

from __future__ import annotations

import logging

import pytest

from axiom.extensions.builtins.attest import grants, service
from axiom.extensions.builtins.attest.logbooks import DEVICE_CLASSES
from axiom.extensions.builtins.attest.skills import SPECS, bind_default
from axiom.infra.principal import PrincipalContext
from axiom.infra.skills import SkillContext

from .test_signing import SITE, _round_check, _sign, person

ME = "@op1:site-a"
OTHER = "@op2:site-a"


def _ack_draft(target, who=OTHER):
    return service.create_ack_draft(site_id=SITE, target=target, origin="human", for_principal=who)


def test_each_kind_signs_under_its_own_rules(attest_db, node_signer):
    rec = _sign(_round_check(), node_signer).record
    statement = service.rules_for(_round_check())
    assert (statement.roles, statement.posture, statement.sign_devices) == (
        ("operator", "senior_operator"), "sso", ("personal", "kiosk", "tablet")
    )  # fmt: skip
    supplement = service.rules_for(
        service.create_supplement_draft(
            site_id=SITE,
            target=rec["attestation_id"],
            reason="addition",
            content={"body": "x"},
            origin="human",
            for_principal=ME,
        )  # fmt: skip
    )
    assert supplement == statement
    ack = service.rules_for(_ack_draft(rec["attestation_id"]))
    assert ack.roles == ("operator", "senior_operator")
    assert ack.posture == "sso" and ack.fresh_within_seconds is None and ack.presence is None
    assert ack.sign_devices == DEVICE_CLASSES
    assert ack.what == "demo_log (acknowledge)"


def test_the_grant_reads_the_rules_from_the_context(attest_db, node_signer):
    rec = _sign(_round_check(), node_signer).record
    p = service.present(_ack_draft(rec["attestation_id"]), modality="screen")
    ctx = service.presentation_context(p.presentation_id)
    assert ctx["kind"] == "ack" and ctx["target"] == rec["attestation_id"]
    assert isinstance(ctx["assurance"], service.SigningRules)
    # The log's posture floor applies: an attested session is below demo_log's sso floor.
    with pytest.raises(grants.GrantRefused, match="posture") as exc:
        grants.mint(
            p.presentation_id,
            auth=grants.Authentication(
                principal=PrincipalContext(handle=OTHER, posture="attested", assured=True),
                auth_time=None,
            ),
            device=grants.Device(device_id="d", device_class="personal"),
            signer=node_signer,
        )
    assert exc.value.code == "posture"


def test_the_card_shows_exactly_what_is_acknowledged(attest_db, node_signer):
    rec = _sign(_round_check(note="all well"), node_signer).record
    d = _ack_draft(rec["attestation_id"])
    draft = service.draft(d)
    assert (draft["kind"], draft["target"], draft["reason"], draft["meaning"]) == (
        "ack", rec["attestation_id"], None, "acknowledged"
    )  # fmt: skip
    assert draft["content"] == {"title": "Acknowledged: Round check", "fields": {}}
    p = service.present(d)
    assert p.rendered.splitlines() == [
        f"Acknowledge #{rec['seq']} ({rec['digest'][:8]})",
        "  Original: ROUND_CHECK · Round check",
        "  reading: ok",
        "  Walkdown performed: yes",
        "  note: all well",
    ]
    assert p.forms["line"] == p.rendered.splitlines()[0]
    assert p.presenter.endswith(".ack@1")
    assert p.statement["target_digest"] == rec["digest"] and p.statement["entry_type"] is None
    assert "Acknowledge entry" in p.speakable and "Round check" in p.speakable


def test_an_acknowledgement_is_in_the_thread_and_announced_as_signed(attest_db, node_signer):
    from axiom.infra.bus import get_default_eventbus

    rec = _sign(_round_check(), node_signer).record
    seen = []
    sub = get_default_eventbus().subscribe(
        "attest.demo_log.*", lambda s, p: seen.append((s, p)), source="t"
    )
    try:
        a = _sign(_ack_draft(rec["attestation_id"]), node_signer, person(handle=OTHER)).record
    finally:
        get_default_eventbus().unsubscribe(sub)
    assert [s for s, _ in seen] == ["attest.demo_log.signed"]
    assert seen[0][1]["kind"] == "ack" and seen[0][1]["target"] == rec["attestation_id"]
    assert [t["kind"] for t in service.thread(rec["attestation_id"])] == ["statement", "ack"]
    assert service.brief(a)["kind"] == "ack"


@pytest.fixture
def state(tmp_path, node_signer):
    from axiom.extensions.builtins.attest import roles, signing

    roles.grant(OTHER, SITE, "operator", by="@admin:site-a", state_dir=tmp_path)
    signing.set_provider(lambda: node_signer)
    yield tmp_path
    signing.reset_provider()


def _ctx(state_dir, answers=None, *, handle=OTHER, surface="cli"):
    replies = list(answers or [])
    return SkillContext(
        registry=bind_default(), state_dir=state_dir, logger=logging.getLogger("test.l3"),
        principal=PrincipalContext(handle=handle, posture="sso", assured=True),
        user_prompt=(lambda _t: replies.pop(0)) if answers is not None else None,
        surface=surface,
    )  # fmt: skip


def test_attest_ack_and_unacknowledged_skills(attest_db, state, node_signer):
    rec = _sign(_round_check(), node_signer).record
    c = _ctx(state)
    before = c.registry.invoke("attest.unacknowledged", {"site": SITE, "logbook": "demo_log"}, c)
    assert before.ok and [r["attestation_id"] for r in before.value["records"]] == [
        rec["attestation_id"]
    ]
    held = _ctx(state, ["hold"])
    r = held.registry.invoke("attest.ack", {"attestation_id": rec["attestation_id"]}, held)
    assert r.ok and r.value["status"] == "hold"
    signed = _ctx(state, ["sign"])
    r = signed.registry.invoke("attest.ack", {"attestation_id": rec["attestation_id"]}, signed)
    assert r.ok, r.errors
    after = c.registry.invoke("attest.unacknowledged", {"site": SITE, "logbook": "demo_log"}, c)
    assert after.ok and after.value["records"] == []
    mine = c.registry.invoke(
        "attest.unacknowledged", {"site": SITE, "logbook": "demo_log", "by": "me"}, c
    )
    assert mine.ok and mine.value["by"] == OTHER and mine.value["records"] == []
    other = c.registry.invoke(
        "attest.unacknowledged", {"site": SITE, "logbook": "demo_log", "by": "@op3:site-a"}, c
    )
    assert [x["attestation_id"] for x in other.value["records"]] == [rec["attestation_id"]]
    missing = c.registry.invoke("attest.ack", {"attestation_id": "nope"}, _ctx(state, ["sign"]))
    assert not missing.ok and "no record" in missing.errors[0]


def test_ack_skills_declare_risk_and_requires():
    by_name = {s.name: s for s in SPECS}
    assert by_name["attest.ack"].risk == "reversible-write" and by_name["attest.ack"].requires
    assert by_name["attest.ack"].surfaces == ("cli",)
    assert by_name["attest.unacknowledged"].risk == "read"
    assert by_name["attest.unacknowledged"].side_effects is False
