# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""S3 Confirm and sign (#1818): the person fills the readings, asks with a correction, signs with
a fresh grant at the desk; presence decides where; the command line signs what its posture
allows."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from .conftest import (
    ANN,
    LOG,
    ROOM,
    SECRET,
    SITE,
    auth,
    draft,
    keys,
    open_session,
    readings,
    signatory,
)


def _present_and_ask(draft_id):
    from axiom.extensions.builtins.attest import service

    pres = service.present(draft_id, modality="screen")
    asked = service.respond(pres.presentation_id, principal=signatory(ANN).principal, answer="ask",
                            via="screen", correction=readings(flow="12.6"))  # fmt: skip
    return pres, asked.next_presentation


def _mint(pres, device, code=None):
    from axiom.extensions.builtins.attest import grants, signing

    return grants.mint(pres.presentation_id, auth=auth(ANN),
                       device=grants.Device(device_id=device, device_class="personal"),
                       presence_code=code, signer=signing.signer())  # fmt: skip


@pytest.mark.acceptance("LB-E2E-A4")
def test_lb_e2e_a4_fill_ask_correct_present_again_and_sign_on_the_desk(world):
    from axiom.extensions.builtins.attest import service, signing

    open_session(world)
    d = draft(ANN, "CHECK", readings())
    first, again = _present_and_ask(d)
    assert again is not None and again.digest != first.digest
    with pytest.raises(service.AttestRefused):
        service.sign(first.presentation_id, signatory=signatory(ANN), signer=signing.signer())
    service.respond(
        again.presentation_id, principal=signatory(ANN).principal, answer="sign", via="screen"
    )
    rec = service.sign(again.presentation_id, signatory=signatory(ANN), signer=signing.signer(),
                       source="screen", grant=_mint(again, "desk-1"), grant_keys=keys()).record  # fmt: skip
    assert rec["content"]["fields"]["readings"]["flow"]["value"] == "12.6"
    assert rec["content"]["fields"]["readings"]["flow"]["unit"] == "L/min"
    ev = rec["evidence"]
    assert ev["presentation_digest"] == again.digest and ev["response"]["answer"] == "sign"
    assert rec["assurance"]["device_id"] == "desk-1"
    assert rec["assurance"]["presence"] == {"location": ROOM, "proof": "enrolment"}
    assert rec["occurred_at"] and rec["recorded_at"]


@pytest.mark.acceptance("LB-E2E-A5")
def test_lb_e2e_a5_presence_refuses_a_device_elsewhere_and_accepts_the_current_code(world):
    from axiom.extensions.builtins.attest import grants, presence, service, signing

    open_session(world)
    pres = service.present(draft(ANN, "CHECK", readings()), modality="screen")
    service.respond(
        pres.presentation_id, principal=signatory(ANN).principal, answer="sign", via="screen"
    )
    with pytest.raises(grants.GrantRefused, match="presence_required"):
        _mint(pres, "tablet-1")
    with pytest.raises(grants.GrantRefused, match="presence_required"):
        _mint(pres, "tablet-1", code="000000")
    token = _mint(pres, "tablet-1", code=presence.code(SECRET, ROOM, datetime.now(UTC)))
    rec = service.sign(pres.presentation_id, signatory=signatory(ANN), signer=signing.signer(),
                       source="screen", grant=token, grant_keys=keys()).record  # fmt: skip
    assert rec["assurance"]["presence"]["proof"] == "location_code"


def _cli(world, entry_type, fields, answer="sign", posture="attested", occurred_at=None):
    import logging

    from axiom.extensions.builtins.attest.skills import bind_default
    from axiom.infra.principal import PrincipalContext
    from axiom.infra.skills import SkillContext

    ctx = SkillContext(registry=bind_default(), state_dir=world.state_dir,
                       logger=logging.getLogger("e2e"), user_prompt=lambda text: answer,
                       principal=PrincipalContext(handle=ANN.handle, posture=posture, assured=True))  # fmt: skip
    from axiom.extensions.builtins.attest import registry

    params = {"site": "example-site", "logbook": LOG, "type": entry_type, "title": entry_type.title(),
              "meaning": registry.get(LOG).type(entry_type).meanings[0],
              "fields": [f"{k}={v}" for k, v in fields.items()]}  # fmt: skip
    if occurred_at:
        params["occurred_at"] = occurred_at
    return ctx.registry.invoke("attest.new", params, ctx)


@pytest.mark.acceptance("LB-E2E-A6")
def test_lb_e2e_a6_the_command_line_signs_a_note(world):
    r = _cli(world, "NOTE", {"text": "nothing to report"})
    assert r.ok, r.errors
    from axiom.extensions.builtins.attest import service

    assert r.value["status"] == "signed"
    rec = service.record(r.value["attestation_id"])
    assert rec["entry_type"] == "NOTE" and rec["source"] == "cli"
    assert rec["evidence"]["response"]["answer"] == "sign"


@pytest.mark.acceptance("LB-E2E-A6")
def test_lb_e2e_a6_a_readings_field_is_entered_at_the_command_line(world):
    # #1883: ``readings.<instrument>=<value>``, kept as entered, the unit from the instrument.
    from axiom.extensions.builtins.attest import service

    open_session(world)
    r = _cli(world, "CHECK", {"readings.flow": "12.5", "readings.level": "80",
                              "readings.temp": "21.3"}, answer="hold")  # fmt: skip
    # The draft is written; signing a CHECK needs sso and the room, so the web app signs it.
    assert not r.ok and "web app" in r.errors[0], r.errors
    (waiting,) = service.list_drafts(for_principal=ANN.handle, logbook=LOG)
    got = service.draft(waiting["draft_id"])["content"]["fields"]["readings"]
    assert got["flow"]["value"] == "12.5" and got["flow"]["unit"] == "L/min"
    bad = _cli(world, "CHECK", {"readings.nope": "1"}, answer="hold")
    assert not bad.ok and "nope" in bad.errors[0]


@pytest.mark.acceptance("LB-E2E-A6")
def test_lb_e2e_a6_the_command_line_says_where_an_sso_entry_is_signed(world):
    # #1669 (the honest half): the command line reaches at most ``attested``, so it does not ask
    # the person to answer for an entry it cannot sign; it keeps the draft and names the web app.
    from axiom.extensions.builtins.attest import service

    r = _cli(world, "CHANGE", {"text": "the desk setting moved"}, answer="sign")
    assert not r.ok
    msg = r.errors[0]
    assert "sso" in msg and "web app" in msg, msg
    (waiting,) = service.list_drafts(for_principal=ANN.handle, logbook=LOG)
    assert waiting["draft_id"] in msg and waiting["status"] == "open"


def _browser(web, person, *, seen):
    """A browser on this computer: opens the gate's link as ``person``, presses the button, and
    follows the gate's redirect to the command line's listener, as a real browser would."""
    import urllib.request
    from urllib.parse import urlsplit

    from .conftest import as_

    def open_browser(link):
        u = urlsplit(link)
        client = as_(web, person)
        page = client.get(f"{u.path}?{u.query}")
        assert page.status_code == 200, page.text
        form = dict(p.split("=", 1) for p in u.query.split("&"))
        from urllib.parse import unquote

        back = client.post(u.path, data={k: unquote(v) for k, v in form.items()})
        assert back.status_code == 303, back.text
        seen.append(back.headers["location"])
        urllib.request.urlopen(back.headers["location"], timeout=10).read()  # noqa: S310 - loopback
        return True

    return open_browser


def _post_json(web, sent):
    def post(url, body, timeout):
        from urllib.parse import urlsplit

        sent.append(dict(body))
        r = web.post(urlsplit(url).path, json=body)
        return r.status_code, r.json()

    return post


@pytest.mark.acceptance("LB-E2E-A6")
def test_lb_e2e_a6_the_command_line_signs_an_sso_entry_through_the_gate(world, web, caplog):
    # #1669/#1870: the prompt hands the person to the node's own gate, which gives back a
    # single-use proof bound to this presentation and person, with the sign-in time.
    from axiom.extensions.builtins.attest import gate_handoff, service

    seen: list[str] = []
    sent: list[dict] = []
    said: list[str] = []
    gate_handoff.configure(gate_handoff.Handoff(
        face_url="http://gate.example", open_browser=_browser(web, ANN, seen=seen),
        post_json=_post_json(web, sent), say=said.append, timeout_s=10))  # fmt: skip
    try:
        r = _cli(world, "CHANGE", {"text": "the desk setting moved"})
    finally:
        gate_handoff.configure(None)
    assert r.ok, r.errors
    rec = service.record(r.value["attestation_id"])
    assert rec["source"] == "cli" and rec["assurance"]["posture"] == "sso"
    assert rec["assurance"]["grant_id"]
    # The code was spent: the same exchange again is refused.
    again = web.post("/gate/cli/sign/token", json=sent[0])
    assert again.status_code == 400
    # Recorded, and nothing a person reads carries the code or the proof.
    assert any(x.get("skill") == "attest.cli_signing_grant" for x in _chain(world.state_dir))
    code = sent[0]["code"]
    assert all(code not in line for line in said) and code not in caplog.text


def _chain(state_dir):
    from axiom.infra import audit_trail

    return audit_trail.read_chain(audit_trail.audit_path(state_dir))


@pytest.mark.acceptance("LB-E2E-A6")
def test_lb_e2e_a6_a_link_nobody_opens_keeps_the_draft(world, web):
    # Headless: no browser, the link is printed; nothing comes back before the timeout, so nothing
    # is signed and the draft stays in the tray. A gate that refuses (another person) says so.
    from axiom.extensions.builtins.attest import gate_handoff, service

    from .conftest import DEE

    said: list[str] = []
    gate_handoff.configure(gate_handoff.Handoff(
        face_url="http://gate.example", open_browser=lambda link: False,
        post_json=_post_json(web, []), say=said.append, timeout_s=0.5))  # fmt: skip
    try:
        r = _cli(world, "CHANGE", {"text": "nobody came"})
        assert not r.ok and "within" in r.errors[0] and "waits for you" in r.errors[0]
        assert any("/gate/cli/sign?" in line for line in said)
        gate_handoff.configure(gate_handoff.Handoff(
            face_url="http://gate.example", open_browser=_browser(web, DEE, seen=[]),
            post_json=_post_json(web, []), say=said.append, timeout_s=10))  # fmt: skip
        other = _cli(world, "CHANGE", {"text": "someone else at the browser"})
        assert not other.ok and "is for" in other.errors[0], other.errors
    finally:
        gate_handoff.configure(None)
    assert [d["status"] for d in service.list_drafts(for_principal=ANN.handle, logbook=LOG)] == [
        "open", "open"
    ]


@pytest.mark.acceptance("LB-E2E-A7")
def test_lb_e2e_a7_an_earlier_occurred_at_is_kept_and_recorded_at_is_the_servers(world, web):
    # #1813: the person may say when it happened; the server keeps when it was recorded.
    from axiom.extensions.builtins.attest import service, signing

    from .conftest import as_

    r = as_(web, ANN).post("/api/v1/attest/drafts", json={"logbook": LOG, "entry_type": "NOTE",
                           "meaning": "authored", "title": "Late", "fields": {"text": "late"},
                           "occurred_at": "2026-01-01T08:00:00Z"})  # fmt: skip
    assert r.status_code == 201 and r.json().get("occurred_at") == "2026-01-01T08:00:00Z", r.text
    pres = service.present(r.json()["draft_id"], modality="screen")
    assert pres.statement["occurred_at"] == "2026-01-01T08:00:00Z", "the person confirms the time"
    service.respond(pres.presentation_id, principal=signatory(ANN, "attested").principal,
                    answer="sign", via="screen")  # fmt: skip
    rec = service.sign(pres.presentation_id, signatory=signatory(ANN, "attested"),
                       signer=signing.signer()).record  # fmt: skip
    assert rec["occurred_at"].startswith("2026-01-01T08:00:00")
    assert rec["recorded_at"] != rec["occurred_at"]
    b = service.brief(rec)
    assert b["occurred_at"] == rec["occurred_at"] and b["recorded_at"] == rec["recorded_at"]
    future = as_(web, ANN).post("/api/v1/attest/drafts", json={"logbook": LOG, "entry_type": "NOTE",
                                "meaning": "authored", "title": "Soon", "fields": {"text": "x"},
                                "occurred_at": "2999-01-01T00:00:00Z"})  # fmt: skip
    assert future.status_code == 422 and "future" in future.json()["detail"]


@pytest.mark.acceptance("LB-E2E-A7")
def test_lb_e2e_a7_the_command_line_takes_an_earlier_occurred_at(world):
    from axiom.extensions.builtins.attest import service

    r = _cli(world, "NOTE", {"text": "late"}, occurred_at="2026-01-01T08:00:00Z")
    assert r.ok, r.errors
    assert service.record(r.value["attestation_id"])["occurred_at"].startswith("2026-01-01T08:00")
    open_session(world)
    opened = service.records(SITE, LOG)[-1]["occurred_at"]
    early = _cli(world, "CHECK", {"readings.flow": "1", "readings.level": "1", "readings.temp": "1"},
                 answer="hold", occurred_at="2026-01-01T08:00:00Z")  # fmt: skip
    assert not early.ok and "before" in early.errors[0], (opened, early.errors)


@pytest.mark.acceptance("LB-E2E-A4")
def test_lb_e2e_a4_a_reading_is_recorded_as_not_taken(world):
    # #1809: a declared opt-in; "not taken" is its own value, never an empty one, and is said so.
    from axiom.extensions.builtins.attest import service, signing

    open_session(world)
    fields = {"readings": {"flow": "12.5", "level": {"not_taken": True, "reason": "gauge covered"},
                           "temp": "21.3"}}  # fmt: skip
    pres = service.present(draft(ANN, "CHECK", fields), modality="screen")
    assert "Level: not taken (gauge covered)" in pres.rendered
    level = pres.statement["content"]["fields"]["readings"]["level"]
    assert level == {"not_taken": True, "reason": "gauge covered", "unit": "cm"}
    with pytest.raises(service.AttestRefused, match="reason"):
        draft(ANN, "CHECK", {"readings": {"flow": "1", "level": {"not_taken": True}, "temp": "1"}})
    with pytest.raises(service.AttestRefused, match="empty|decimal"):
        draft(ANN, "CHECK", {"readings": {"flow": "1", "level": "", "temp": "1"}})
    assert signing.signer() is not None


def _web_note(web, person, gate_roles):
    """A person whose only roles are the gate's, writing and signing a NOTE over HTTP."""
    import time

    from axiom.webauth import SESSION_COOKIE
    from axiom.webauth.session import issue_session_token
    from axiom.webauth.users import User

    from .conftest import BASE

    u = User(user_id=person.user_id, email=f"{person.user_id}@example.org", name=person.name,
             roles=tuple(gate_roles), site=person.site, attributes={"idp": "entra"})  # fmt: skip
    web.cookies.clear()
    web.cookies.set(SESSION_COOKIE, issue_session_token(u, issuer=BASE, amr=("pwd",),
                                                        auth_time=int(time.time())))  # fmt: skip
    d = web.post("/api/v1/attest/drafts", json={"logbook": LOG, "entry_type": "NOTE",
                 "meaning": "authored", "title": "Gate", "fields": {"text": "by a gate role"}})  # fmt: skip
    assert d.status_code == 201, d.text
    pid = web.post(f"/api/v1/attest/drafts/{d.json()['draft_id']}/present",
                   json={"modality": "screen"}).json()["presentation_id"]  # fmt: skip
    web.post(f"/api/v1/attest/presentations/{pid}/respond", json={"answer": "sign", "via": "screen"})
    g = web.post("/gate/grants", json={"presentation_id": pid})
    assert g.status_code == 200, g.text
    return web.post("/api/v1/attest/sign", json={"presentation_id": pid, "grant": g.json()["grant"]})


@pytest.mark.acceptance("LB-E2E-A6")
def test_lb_e2e_a6_a_gate_role_signs_only_through_a_declared_pair(world, web):
    # #1800, decided by the owner: a site declares explicit pairs, gate role to signing role;
    # nothing is inferred from a role's name; a gate role with no pair cannot sign, and is told
    # why and how to fix it.
    from axiom.extensions.builtins.attest import site_settings

    from .conftest import Person

    fay = Person("fay", SITE, (), "Fay Example")  # no signing role granted in the log
    refused = _web_note(web, fay, ["member"])  # same name as the log's role: still not inferred
    assert refused.status_code == 403
    detail = refused.json()["detail"]
    assert "member" in detail and "attest.gate_roles" in detail, detail
    site_settings.register(lambda site: {"attest": {"gate_roles": {"desk-staff": "member",
                                                                   "admin": "lead"}}}
                           if site == SITE else {})  # fmt: skip
    try:
        ok = _web_note(web, fay, ["desk-staff"])
        assert ok.status_code == 201, ok.text
        # An administration role never pairs into signing authority.
        assert _web_note(web, fay, ["admin"]).status_code == 403
    finally:
        site_settings.unregister()


@pytest.mark.acceptance("LB-E2E-A4")
def test_lb_e2e_a4_a_declared_exact_conversion_keeps_the_value_as_entered(world):
    # #1801, decided by the owner: a conversion is declared and exact; the entry keeps the value
    # as entered and shows the converted value beside it; an undeclared conversion is refused.
    from axiom.extensions.builtins.attest import service

    open_session(world)
    fields = {"readings": {"flow": {"value": "0.2", "unit": "L/s"}, "level": "80", "temp": "21.3"}}
    pres = service.present(draft(ANN, "CHECK", fields), modality="screen")
    flow = pres.statement["content"]["fields"]["readings"]["flow"]
    assert (flow["value"], flow["unit"]) == ("0.2", "L/s")
    assert flow["converted"] == {"value": "12.0", "unit": "L/min", "factor": "60"}
    assert "0.2 L/s (12.0 L/min)" in pres.rendered
    with pytest.raises(service.AttestRefused, match="convert"):
        draft(ANN, "CHECK", {"readings": {"flow": {"value": "3", "unit": "gal/min"}, "level": "80",
                                          "temp": "21.3"}})  # fmt: skip
