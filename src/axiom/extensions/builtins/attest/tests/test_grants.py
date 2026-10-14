# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Signing grants (ADR-146): a short-lived, single-use credential bound to one
presented digest, one person and one device. The browser path signs only with
one; the grant carries how and when the person authenticated into the record."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from axiom.extensions.builtins.attest import grants, registry, service
from axiom.extensions.builtins.attest.grants import Authentication, Device, GrantRefused
from axiom.extensions.builtins.attest.logbooks import parse_logbook
from axiom.extensions.builtins.attest.service import AttestRefused
from axiom.infra.principal import PrincipalContext

from .test_signing import SITE, _round_check, person

DESK = Device(device_id="desk-7", device_class="personal", console_id="console-1")


def auth(handle="@op1:site-a", posture="sso", minutes_ago=1, amr=("pwd",)):
    return Authentication(
        principal=PrincipalContext(handle=handle, posture=posture, assured=True, idp="entra"),
        auth_time=datetime.now(UTC) - timedelta(minutes=minutes_ago),
        amr=amr,
    )


def keys(signer):
    return {signer.key_id: signer.public_bytes}


def presented(draft_id=None, answer="sign"):
    pres = service.present(draft_id or _round_check(), modality="screen")
    service.respond(pres.presentation_id, principal=person().principal, answer=answer, via="screen")
    return pres


def sign_with(pres, token, signer):
    return service.sign(
        pres.presentation_id,
        signatory=person(),
        signer=signer,
        grant=token,
        grant_keys=keys(signer),
    )


# -- minting ---------------------------------------------------------------------------


def test_a_grant_binds_the_digest_person_device_and_a_short_expiry(attest_db, node_signer):
    pres = presented()
    token = grants.mint(pres.presentation_id, auth=auth(), device=DESK, signer=node_signer)
    claims = grants.read(token, keys(node_signer))
    assert claims["digest"] == pres.digest
    assert claims["presentation_id"] == pres.presentation_id
    assert claims["principal"] == "@op1:site-a"
    assert claims["device_id"] == "desk-7" and claims["device_class"] == "personal"
    assert claims["logbook"] == "demo_log" and claims["meaning"] == "performed"
    issued = datetime.fromisoformat(claims["issued_at"].replace("Z", "+00:00"))
    expires = datetime.fromisoformat(claims["expires_at"].replace("Z", "+00:00"))
    assert timedelta(0) < expires - issued <= timedelta(seconds=grants.MAX_TTL_SECONDS)


def test_a_grant_is_only_for_the_person_the_draft_is_for(attest_db, node_signer):
    pres = presented()
    with pytest.raises(GrantRefused, match="is for"):
        grants.mint(
            pres.presentation_id, auth=auth(handle="@op2:site-a"), device=DESK, signer=node_signer
        )


@pytest.mark.parametrize("posture", ["open", "service", "attested"])
def test_a_grant_needs_the_types_posture(attest_db, node_signer, posture):
    pres = presented()
    with pytest.raises(GrantRefused, match="posture"):
        grants.mint(
            pres.presentation_id, auth=auth(posture=posture), device=DESK, signer=node_signer
        )


def test_the_callers_device_class_is_ignored(attest_db, node_signer):
    """Class comes from enrolment. An unenrolled device claiming to be a kiosk
    is a personal session; claiming to be a phone does not make it one either.
    (An enrolled phone is refused: see test_presence.)"""
    pres = presented()
    kiosk_claim = Device(device_id="laptop-9", device_class="kiosk")
    claims = grants.read(
        grants.mint(pres.presentation_id, auth=auth(), device=kiosk_claim, signer=node_signer),
        keys(node_signer),
    )
    assert claims["device_class"] == "personal"
    assert claims["location"] is None


def test_ttl_is_capped(attest_db, node_signer):
    with pytest.raises(ValueError, match="60"):
        grants.mint(
            presented().presentation_id, auth=auth(), device=DESK, signer=node_signer, ttl=300
        )


# -- freshness ---------------------------------------------------------------------------


@pytest.fixture
def fresh_logbook(attest_db):
    registry.register(
        parse_logbook(
            {
                "logbook": {"id": "fresh_log", "version": "1", "display": "Fresh"},
                "type": [
                    {
                        "id": "RELEASE",
                        "meanings": ["approved"],
                        "roles": ["operator"],
                        "assurance": {"posture": "sso", "fresh_within": "5m"},
                        "confirm": {"modalities_any": ["screen"]},
                        "fields": [{"id": "what", "type": "text", "required": True}],
                    }
                ],
            },
            source="test:fresh",
        )
    )
    return service.create_draft(
        site_id=SITE,
        logbook="fresh_log",
        entry_type="RELEASE",
        meaning="approved",
        content={"title": "Release", "fields": {"what": "batch 4"}},
        origin="human",
        for_principal="@op1:site-a",
    )


def test_fresh_within_refuses_an_old_sign_in(fresh_logbook, node_signer):
    pres = presented(fresh_logbook)
    with pytest.raises(GrantRefused, match="reauth_required"):
        grants.mint(
            pres.presentation_id, auth=auth(minutes_ago=30), device=DESK, signer=node_signer
        )


def test_fresh_within_refuses_an_unknown_sign_in_time(fresh_logbook, node_signer):
    pres = presented(fresh_logbook)
    a = Authentication(principal=auth().principal, auth_time=None, amr=())
    with pytest.raises(GrantRefused, match="reauth_required"):
        grants.mint(pres.presentation_id, auth=a, device=DESK, signer=node_signer)


def test_a_fresh_sign_in_signs_and_the_record_says_so(fresh_logbook, node_signer):
    pres = presented(fresh_logbook)
    token = grants.mint(
        pres.presentation_id, auth=auth(minutes_ago=2), device=DESK, signer=node_signer
    )
    rec = sign_with(pres, token, node_signer).record
    a = rec["assurance"]
    assert a["posture"] == "sso" and a["idp"] == "entra"
    assert a["fresh_within_met"] is True
    assert a["amr"] == ["pwd"]
    assert a["grant_id"] and a["device_id"] == "desk-7" and a["console_id"] == "console-1"


def test_a_type_with_fresh_within_cannot_be_signed_without_a_grant(fresh_logbook, node_signer):
    pres = presented(fresh_logbook)
    with pytest.raises(AttestRefused, match="signing grant"):
        service.sign(pres.presentation_id, signatory=person(), signer=node_signer)


# -- using a grant ---------------------------------------------------------------------------


def test_a_used_grant_is_refused_by_the_database_not_just_the_draft(attest_db, node_signer):
    """The draft's status already stops a second signing; the grant-use table
    is the second line, so exercise it on its own: record the grant as used,
    then try to sign with it."""
    from datetime import UTC, datetime

    from axiom.extensions.builtins.attest import store
    from axiom.extensions.builtins.attest.db_models import AttestGrantUse

    pres = presented()
    token = grants.mint(pres.presentation_id, auth=auth(), device=DESK, signer=node_signer)
    claims = grants.read(token, keys(node_signer))
    with store.session_scope() as s:
        s.add(
            AttestGrantUse(
                grant_id=claims["grant_id"],
                presentation_id=pres.presentation_id,
                principal="@op1:site-a",
                used_at=datetime.now(UTC),
            )
        )
        s.commit()
    with pytest.raises(AttestRefused, match="already used"):
        sign_with(pres, token, node_signer)
    assert service.records(SITE, "demo_log") == []


def test_a_grant_for_one_presentation_does_not_sign_another(attest_db, node_signer):
    a, b = presented(), presented()
    token = grants.mint(a.presentation_id, auth=auth(), device=DESK, signer=node_signer)
    with pytest.raises(AttestRefused, match="not for this presentation"):
        sign_with(b, token, node_signer)


def test_an_expired_grant_is_refused(attest_db, node_signer, monkeypatch):
    pres = presented()
    token = grants.mint(pres.presentation_id, auth=auth(), device=DESK, signer=node_signer)
    later = datetime.now(UTC) + timedelta(seconds=grants.MAX_TTL_SECONDS + 1)
    monkeypatch.setattr(grants, "_now", lambda: later)
    with pytest.raises(AttestRefused, match="expired"):
        sign_with(pres, token, node_signer)


def test_a_tampered_grant_is_refused(attest_db, node_signer):
    pres = presented()
    token = grants.mint(pres.presentation_id, auth=auth(), device=DESK, signer=node_signer)
    body, sig = token.split(".")
    forged = grants._b64(grants._unb64(body).replace(b'"personal"', b'"kiosk"')) + "." + sig
    with pytest.raises(AttestRefused, match="signature"):
        sign_with(pres, forged, node_signer)


def test_a_grant_is_for_the_person_who_signs(attest_db, node_signer):
    pres = presented()
    token = grants.mint(pres.presentation_id, auth=auth(), device=DESK, signer=node_signer)
    other = person(handle="@op2:site-a")
    with pytest.raises(AttestRefused, match="is for"):
        service.sign(
            pres.presentation_id,
            signatory=other,
            signer=node_signer,
            grant=token,
            grant_keys=keys(node_signer),
        )


def test_a_grant_still_needs_the_persons_sign_answer(attest_db, node_signer):
    pres = presented(answer="hold")
    token = grants.mint(pres.presentation_id, auth=auth(), device=DESK, signer=node_signer)
    with pytest.raises(AttestRefused, match="only 'sign' signs"):
        sign_with(pres, token, node_signer)
