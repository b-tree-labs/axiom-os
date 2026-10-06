# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Confirmation against real Postgres (ADR-144): what was shown, through what,
and what the person answered, all kept as evidence. Only an answered
``sign`` signs, and only for the content that was shown."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import text

from axiom.extensions.builtins.attest import registry, service, store
from axiom.extensions.builtins.attest.logbooks import parse_logbook
from axiom.extensions.builtins.attest.service import AttestRefused

from .test_signing import LOGBOOK, SITE, _round_check, person


def _note():
    return service.create_draft(
        site_id=SITE,
        logbook=LOGBOOK,
        entry_type="NOTE",
        meaning="authored",
        content={"title": "note", "fields": {"text": "hello"}},
        origin="human",
        for_principal="@op1:site-a",
    )


def _answer(pres, answer="sign", via="cli", who=None, **kw):
    return service.respond(
        pres.presentation_id, principal=(who or person()).principal, answer=answer, via=via, **kw
    )


# -- presentations -------------------------------------------------------------------


def test_a_presentation_keeps_every_form_the_person_could_see(attest_db):
    pres = service.present(_round_check(), modality="screen")
    assert pres.modality == "screen"
    assert pres.presenter == "demo_log.round_check@1"
    assert set(pres.forms) == {"strip", "line", "card", "page"}
    assert pres.rendered == pres.forms["card"]
    assert pres.speakable.startswith("Round check.")
    assert "Walkdown performed: yes" in pres.speakable
    assert pres.expires_at is not None


def test_a_modality_the_type_does_not_allow_is_refused(attest_db):
    with pytest.raises(AttestRefused, match="may not be presented by 'voice'"):
        service.present(_note(), modality="voice")  # NOTE uses the defaults: no voice


# -- responses -------------------------------------------------------------------------


def test_sign_needs_an_answer(attest_db, node_signer):
    pres = service.present(_round_check())
    with pytest.raises(AttestRefused, match="no answer"):
        service.sign(pres.presentation_id, signatory=person(), signer=node_signer)


def test_the_latest_answer_decides(attest_db, node_signer):
    pres = service.present(_round_check())
    _answer(pres, "hold")
    with pytest.raises(AttestRefused, match="answered 'hold'"):
        service.sign(pres.presentation_id, signatory=person(), signer=node_signer)
    _answer(pres, "sign")
    assert service.sign(pres.presentation_id, signatory=person(), signer=node_signer).record


def test_every_answer_is_kept_as_evidence(attest_db, node_signer):
    pres = service.present(_round_check())
    _answer(pres, "hold")
    _answer(pres, "sign", latency_ms=1840, console_id="console-1")
    with store.session_scope() as s:
        rows = s.execute(
            text("SELECT answer FROM attest_responses WHERE presentation_id = :p ORDER BY at"),
            {"p": pres.presentation_id},
        ).scalars()
        assert list(rows) == ["hold", "sign"]
    rec = service.sign(pres.presentation_id, signatory=person(), signer=node_signer).record
    assert rec["evidence"]["response"]["latency_ms"] == 1840
    assert rec["evidence"]["response"]["console_id"] == "console-1"


def test_responses_are_append_only(attest_db):
    _answer(service.present(_round_check()), "hold")
    with pytest.raises(Exception, match="append-only"):
        with store.session_scope() as s:
            s.execute(text("UPDATE attest_responses SET answer = 'sign'"))
            s.commit()


def test_a_spoken_confirm_signs_only_where_the_logbook_allows_it(attest_db, node_signer):
    # ROUND_CHECK allows voice confirmation.
    pres = service.present(_round_check(), modality="voice")
    _answer(pres, "sign", via="voice", transcript="confirm", audio_sha256="ab" * 32)
    rec = service.sign(pres.presentation_id, signatory=person(), signer=node_signer).record
    assert rec["evidence"]["response"]["transcript"] == "confirm"


def test_a_spoken_confirm_is_refused_where_the_logbook_does_not_allow_voice(attest_db):
    pres = service.present(_note(), modality="screen")
    with pytest.raises(AttestRefused, match="cannot be answered by 'voice'"):
        _answer(pres, "sign", via="voice")


def test_an_expired_presentation_cannot_be_answered(attest_db, monkeypatch):
    pres = service.present(_round_check())
    later = pres.expires_at + timedelta(seconds=1)
    monkeypatch.setattr(service, "_now", lambda: later)
    with pytest.raises(AttestRefused, match="expired"):
        _answer(pres, "sign")


def test_ask_with_a_correction_yields_a_new_presentation_and_voids_the_old(attest_db, node_signer):
    d = _round_check()
    first = service.present(d)
    r = _answer(first, "ask", correction={"reading": "4.3 bar"})
    nxt = r.next_presentation
    assert nxt is not None and nxt.digest != first.digest
    assert "4.3 bar" in nxt.rendered
    assert service.draft(d)["provenance"]["reading"] == "@op1:site-a"
    with pytest.raises(AttestRefused, match="changed since"):
        _answer(first, "sign")
    _answer(nxt, "sign")
    rec = service.sign(nxt.presentation_id, signatory=person(), signer=node_signer).record
    assert rec["content"]["fields"]["reading"] == "4.3 bar"


def test_one_answer_signs_once(attest_db, node_signer):
    pres = service.present(_round_check())
    _answer(pres, "sign")
    service.sign(pres.presentation_id, signatory=person(), signer=node_signer)
    with pytest.raises(AttestRefused, match="already signed"):
        service.sign(pres.presentation_id, signatory=person(), signer=node_signer)


# -- modalities_all ------------------------------------------------------------------------


@pytest.fixture
def dual_logbook(attest_db):
    registry.register(
        parse_logbook(
            {
                "logbook": {"id": "dual_log", "version": "1", "display": "Dual"},
                "type": [
                    {
                        "id": "HANDOVER",
                        "meanings": ["acknowledged"],
                        "roles": ["operator"],
                        "fields": [{"id": "summary", "type": "text", "required": True}],
                        "confirm": {
                            "modalities_any": ["screen", "voice"],
                            "modalities_all": ["screen", "voice"],
                            "voice_confirm_allowed": True,
                        },
                    }
                ],
            },
            source="test:dual",
        )
    )
    return service.create_draft(
        site_id=SITE,
        logbook="dual_log",
        entry_type="HANDOVER",
        meaning="acknowledged",
        content={"title": "Handover", "fields": {"summary": "quiet shift"}},
        origin="human",
        for_principal="@op1:site-a",
    )


def test_every_required_modality_must_present_the_same_content(dual_logbook, node_signer):
    screen = service.present(dual_logbook, modality="screen")
    _answer(screen, "sign", via="screen")
    with pytest.raises(AttestRefused, match=r"must also be presented by \['voice'\]"):
        service.sign(screen.presentation_id, signatory=person(), signer=node_signer)
    service.present(dual_logbook, modality="voice")
    rec = service.sign(screen.presentation_id, signatory=person(), signer=node_signer).record
    assert sorted(p["modality"] for p in rec["evidence"]["presented"]) == ["screen", "voice"]
