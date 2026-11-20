# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Acceptance tests for unit L3: acknowledge an entry.

Ticket: docs/working/logbook-units/L3.md in the product repo that holds the plan, built against
`docs/working/design-note-l3-acknowledge.md`. Rows A1 to A6 are Axiom's; A7 (the Acknowledge
action on the entry detail) is appkit's, in `frontend/src/logbookAcceptance/l3.test.tsx`.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

pytest.importorskip("fastapi")

from axiom.extensions.builtins.attest import devices, grants, obligations, registry, service  # noqa: E402
from axiom.extensions.builtins.attest import roles as role_store  # noqa: E402
from axiom.extensions.builtins.attest import site_settings, store  # noqa: E402
from axiom.extensions.builtins.attest.grants import Authentication, Device, GrantRefused  # noqa: E402
from axiom.extensions.builtins.attest.logbooks import parse_logbook  # noqa: E402
from axiom.extensions.builtins.attest.service import AttestRefused  # noqa: E402
from axiom.extensions.builtins.attest.skills import SPECS, bind_default  # noqa: E402
from axiom.extensions.builtins.attest.tests.test_api import OP1, OP2, as_, sign_round  # noqa: E402
from axiom.extensions.builtins.attest.tests.test_signing import person  # noqa: E402
from axiom.infra.principal import PrincipalContext  # noqa: E402
from axiom.infra.skills import SkillContext  # noqa: E402

SITE = "site-a"
ME = "@op1:site-a"
OTHER = "@op2:site-a"


def _keys(signer):
    return {signer.key_id: signer.public_bytes}


def _sign_draft(draft_id, signer, who=None, modality="cli"):
    who = who or person()
    p = service.present(draft_id, modality=modality)
    service.respond(p.presentation_id, principal=who.principal, answer="sign", via=modality)
    return service.sign(p.presentation_id, signatory=who, signer=signer).record


def original(signer, reading="ok", who=None):
    who = who or person()
    d = service.create_draft(
        site_id=SITE, logbook="demo_log", entry_type="ROUND_CHECK", meaning="performed",
        content={"title": "Round check", "fields": {"reading": reading, "walkdown": True}},
        origin="human", for_principal=who.principal.handle,
    )  # fmt: skip
    return _sign_draft(d, signer, who)


def reader(handle=OTHER, roles=("operator",)):
    return person(handle=handle, roles=roles)


def ack(signer, target, who=None):
    who = who or reader()
    d = service.create_ack_draft(
        site_id=SITE, target=target, origin="human", for_principal=who.principal.handle
    )
    return _sign_draft(d, signer, who)


def _row(attestation_id):
    # Through the extension's own session (it sets the attest search_path); see test_l2.
    with store.session_scope() as s:
        return s.execute(
            text(
                "SELECT record::text AS record, digest, kind, target FROM attest_records WHERE attestation_id = :id"
            ),
            {"id": attestation_id},
        ).one()


# -- acceptance rows ----------------------------------------------------------------------


@pytest.mark.acceptance("LB-L3-A1")
def test_l3_a1_an_acknowledgement_binds_to_the_bytes_and_leaves_them_alone(attest_db, node_signer):
    rec = original(node_signer)
    before = _row(rec["attestation_id"])
    assert service.verify_logbook(SITE, "demo_log", _keys(node_signer)).ok
    a = ack(node_signer, rec["attestation_id"])
    assert a["kind"] == "ack" and a["meaning"] == "acknowledged"
    assert a["target"] == rec["attestation_id"] and a["target_digest"] == rec["digest"]
    assert a["entry_type"] is None and "interval" not in a
    assert a["content"] == {"title": "Acknowledged: Round check", "fields": {}}
    assert a["signer"]["principal"] == OTHER
    assert _row(rec["attestation_id"]) == before
    assert _row(a["attestation_id"]).kind == "ack"
    report = service.verify_logbook(SITE, "demo_log", _keys(node_signer))
    assert report.ok and report.checked == 2
    assert [(b["kind"], b["signer"]) for b in service.thread(rec["attestation_id"])] == [
        ("statement", ME), ("ack", OTHER)
    ]  # fmt: skip
    # A supplement may be acknowledged too, and the acknowledgement names it.
    sd = service.create_supplement_draft(
        site_id=SITE, target=rec["attestation_id"], reason="addition", content={"body": "more"},
        origin="human", for_principal=ME,
    )  # fmt: skip
    sup = _sign_draft(sd, node_signer)
    a2 = ack(node_signer, sup["attestation_id"])
    assert a2["target"] == sup["attestation_id"] and a2["target_digest"] == sup["digest"]


@pytest.mark.acceptance("LB-L3-A2")
def test_l3_a2_a_second_acknowledgement_by_the_same_person_is_refused(attest_db, node_signer):
    rec = original(node_signer)
    first = ack(node_signer, rec["attestation_id"])
    with pytest.raises(AttestRefused, match=f"already acknowledged by you at #{first['seq']}"):
        service.create_ack_draft(
            site_id=SITE, target=rec["attestation_id"], origin="human", for_principal=OTHER
        )
    # Another person may still acknowledge it.
    third = ack(node_signer, rec["attestation_id"], who=reader("@op3:site-a"))
    assert third["signer"]["principal"] == "@op3:site-a"
    # Two open drafts by one person: the second to sign is refused inside the transaction.
    d1 = service.create_ack_draft(
        site_id=SITE, target=rec["attestation_id"], origin="human", for_principal="@op4:site-a"
    )
    d2 = service.create_ack_draft(
        site_id=SITE, target=rec["attestation_id"], origin="human", for_principal="@op4:site-a"
    )
    _sign_draft(d1, node_signer, reader("@op4:site-a"))
    with pytest.raises(AttestRefused, match="already acknowledged"):
        _sign_draft(d2, node_signer, reader("@op4:site-a"))
    assert len(service.records(SITE, "demo_log")) == 4


@pytest.mark.acceptance("LB-L3-A3")
def test_l3_a3_the_signer_cannot_acknowledge_their_own_entry_and_admin_never_counts(
    attest_db, node_signer
):
    rec = original(node_signer)
    with pytest.raises(AttestRefused, match="own entry"):
        service.create_ack_draft(
            site_id=SITE, target=rec["attestation_id"], origin="human", for_principal=ME
        )
    # Only an administration role, or no role at all: refused at signing.
    for who in (reader(roles=("admin",)), reader(roles=())):
        d = service.create_ack_draft(
            site_id=SITE, target=rec["attestation_id"], origin="human", for_principal=OTHER
        )
        with pytest.raises(AttestRefused, match="holds no role"):
            _sign_draft(d, node_signer, who)
    # A role that signs another type of the log is enough: NOTE's operator may acknowledge a
    # ROUND_CHECK, and the senior_operator role (ROUND_CHECK only) may acknowledge a NOTE.
    note = service.create_draft(
        site_id=SITE, logbook="demo_log", entry_type="NOTE", meaning="authored",
        content={"title": "A note", "fields": {"text": "hello"}}, origin="human", for_principal=ME,
    )  # fmt: skip
    n = _sign_draft(note, node_signer, person(posture="sso"))
    a = ack(node_signer, n["attestation_id"], who=reader(roles=("senior_operator",)))
    assert a["signer"]["authority"]["roles"] == ["senior_operator"]
    assert len(service.records(SITE, "demo_log")) == 3


@pytest.mark.acceptance("LB-L3-A4")
def test_l3_a4_a_phone_can_acknowledge_but_still_cannot_sign_a_statement(attest_db, node_signer):
    devices.enroll(site_id=SITE, device_id="phone-1", device_class="phone", location=None,
                   mobility="portable", by="@admin:site-a")  # fmt: skip
    phone = Device(device_id="phone-1", device_class="phone")
    rec = original(node_signer)
    me = reader()
    auth = Authentication(principal=me.principal, auth_time=datetime.now(UTC) - timedelta(hours=3))

    d = service.create_ack_draft(
        site_id=SITE, target=rec["attestation_id"], origin="human", for_principal=OTHER
    )
    p = service.present(d, modality="screen")
    service.respond(p.presentation_id, principal=me.principal, answer="sign", via="screen")
    token = grants.mint(p.presentation_id, auth=auth, device=phone, signer=node_signer)
    a = service.sign(
        p.presentation_id,
        signatory=me,
        signer=node_signer,
        grant=token,
        grant_keys=_keys(node_signer),
    ).record
    assert a["kind"] == "ack" and a["assurance"]["device_class"] == "phone"

    sd = service.create_draft(
        site_id=SITE, logbook="demo_log", entry_type="ROUND_CHECK", meaning="performed",
        content={"title": "Round check", "fields": {"reading": "x", "walkdown": True}},
        origin="human", for_principal=OTHER,
    )  # fmt: skip
    sp = service.present(sd, modality="screen")
    service.respond(sp.presentation_id, principal=me.principal, answer="sign", via="screen")
    with pytest.raises(GrantRefused, match="phone may not sign") as exc:
        grants.mint(sp.presentation_id, auth=auth, device=phone, signer=node_signer)
    assert exc.value.code == "device_not_allowed"


@pytest.fixture
def known_callers(monkeypatch):
    from axiom.extensions.builtins.attest import proposals

    monkeypatch.setattr(proposals, "_agent_names", lambda: {"clerk"})


def _ctx(state_dir, *, handle=OTHER, actor=None, surface="cli", answers=None, posture="sso"):
    replies = list(answers or [])
    return SkillContext(
        registry=bind_default(), state_dir=state_dir, logger=logging.getLogger("test.l3"),
        principal=PrincipalContext(handle=handle, posture=posture, assured=posture != "service"),
        actor=actor, surface=surface,
        user_prompt=(lambda _t: replies.pop(0)) if answers is not None else None,
    )  # fmt: skip


@pytest.mark.acceptance("LB-L3-A5")
def test_l3_a5_only_a_person_at_the_cli_acknowledges_and_only_an_entry(
    attest_db, node_signer, tmp_path, known_callers
):
    from axiom.extensions.builtins.attest import signing

    by_name = {s.name: s for s in SPECS}
    assert by_name["attest.ack"].surfaces == ("cli",)
    assert set(by_name["attest.unacknowledged"].surfaces) == {"cli", "mcp", "agent_tool"}
    assert by_name["attest.unacknowledged"].side_effects is False
    rec = original(node_signer)
    role_store.grant(OTHER, SITE, "operator", by="@admin:site-a", state_dir=tmp_path)
    signing.set_provider(lambda: node_signer)
    try:
        c = _ctx(tmp_path)
        nobody = c.registry.invoke("attest.ack", {"attestation_id": rec["attestation_id"]}, c)
        assert not nobody.ok and "nothing was signed" in nobody.errors[0]
        machine = _ctx(tmp_path, answers=["sign"], posture="service")
        refused = machine.registry.invoke(
            "attest.ack", {"attestation_id": rec["attestation_id"]}, machine
        )
        assert not refused.ok and "posture" in refused.errors[0]
        c = _ctx(tmp_path, answers=["sign"])
        signed = c.registry.invoke("attest.ack", {"attestation_id": rec["attestation_id"]}, c)
        assert signed.ok, signed.errors
        a = service.record(signed.value["attestation_id"])
        assert a["kind"] == "ack" and a["signer"]["principal"] == OTHER
    finally:
        signing.reset_provider()
    # An ack, co-sign or seal cannot be acknowledged.
    with pytest.raises(AttestRefused, match="cannot be acknowledged"):
        service.create_ack_draft(
            site_id=SITE, target=a["attestation_id"], origin="human", for_principal="@op3:site-a"
        )
    for i, kind in enumerate(("cosign", "seal")):
        with store.session_scope() as s:
            s.execute(
                text(
                    "INSERT INTO attest_records (attestation_id, site_id, logbook, seq, digest, prev_digest, "
                    "kind, meaning, entry_type, signer, occurred_at, recorded_at, record) VALUES "
                    "(:id, :site, 'other_log', :seq, repeat(:d, 64), repeat('0', 64), :kind, 'approved', NULL, "
                    ":me, now(), now(), CAST(:rec AS jsonb))"
                ),
                {
                    "id": f"2222222{i}-2222-2222-2222-222222222222",
                    "site": SITE,
                    "seq": i + 1,
                    "d": str(i),
                    "kind": kind,
                    "me": ME,
                    "rec": json.dumps({"kind": kind}),
                },
            )
            s.commit()
        with pytest.raises(AttestRefused, match="cannot be acknowledged"):
            service.create_ack_draft(site_id=SITE, target=f"2222222{i}-2222-2222-2222-222222222222",
                                     origin="human", for_principal=OTHER)  # fmt: skip
    for bad in ("00000000-0000-0000-0000-000000000000", None, ""):
        with pytest.raises(AttestRefused, match="no record"):
            service.create_ack_draft(site_id=SITE, target=bad, origin="human", for_principal=OTHER)
    with pytest.raises(AttestRefused, match="no record"):
        service.create_ack_draft(
            site_id="site-b", target=rec["attestation_id"], origin="human", for_principal=OTHER
        )
    with pytest.raises(AttestRefused, match="no record"):
        service.create_ack_draft(site_id=SITE, target=rec["attestation_id"], origin="human",
                                 for_principal=OTHER, logbook="other_log")  # fmt: skip
    # The record is untouched by any of it.
    for statement in (
        "UPDATE attest_records SET meaning = 'acknowledged' WHERE attestation_id = :id",
        "DELETE FROM attest_records WHERE attestation_id = :id",
    ):
        with pytest.raises(Exception, match="append"), store.session_scope() as s:
            s.execute(text(statement), {"id": rec["attestation_id"]})
            s.commit()


WATCH = {
    "logbook": {"id": "watch_log", "version": "1"},
    "interval": {"run": {"opens": ["START"], "closes": ["STOP"]}},
    "type": [
        {"id": "START", "meanings": ["authored"], "roles": ["operator"], "requires_interval": "none_open"},
        {"id": "CHECK", "meanings": ["performed"], "roles": ["operator"], "requires_interval": "run"},
        {"id": "STOP", "meanings": ["authored"], "roles": ["operator"], "requires_interval": "run"},
    ],
    "obligation": [
        {"id": "check_cadence", "type": "CHECK", "every": {"default": 30}, "warn_before": {"default": 5},
         "while": "interval.run.open", "notify": ["operator"], "required": True},
    ],
}  # fmt: skip


@pytest.mark.acceptance("LB-L3-A6")
def test_l3_a6_unacknowledged_is_exact_and_an_ack_meets_no_obligation(
    attest_db, node_signer, monkeypatch
):
    r1 = original(node_signer)
    r2 = original(node_signer, reading="two")
    ids = lambda rows: [r["attestation_id"] for r in rows]  # noqa: E731
    assert ids(service.unacknowledged(SITE, "demo_log")) == [
        r1["attestation_id"],
        r2["attestation_id"],
    ]
    a = ack(node_signer, r1["attestation_id"])
    assert ids(service.unacknowledged(SITE, "demo_log")) == [r2["attestation_id"]]
    # By a named person: what they have not acknowledged, whoever else has.
    assert ids(service.unacknowledged(SITE, "demo_log", by="@op3:site-a")) == [
        r1["attestation_id"],
        r2["attestation_id"],
    ]
    assert ids(service.unacknowledged(SITE, "demo_log", by=OTHER)) == [r2["attestation_id"]]
    assert ids(service.unacknowledged(SITE, "demo_log", since_seq=r2["seq"])) == [
        r2["attestation_id"]
    ]
    # Acknowledgements are not themselves listed; a supplement is, until acknowledged.
    sd = service.create_supplement_draft(site_id=SITE, target=r2["attestation_id"], reason="addition",
                                         content={"body": "x"}, origin="human", for_principal=ME)  # fmt: skip
    sup = _sign_draft(sd, node_signer)
    assert ids(service.unacknowledged(SITE, "demo_log")) == [
        r2["attestation_id"],
        sup["attestation_id"],
    ]
    assert a["attestation_id"] not in ids(service.unacknowledged(SITE, "demo_log"))
    assert all(
        b["kind"] in ("statement", "supplement") for b in service.unacknowledged(SITE, "demo_log")
    )

    # An acknowledgement never meets a check obligation.
    t0 = datetime(2026, 10, 1, 8, 0, tzinfo=UTC)
    now = {"t": t0}
    monkeypatch.setattr(service, "_now", lambda: now["t"])
    registry.register(parse_logbook(WATCH, source="test:watch"))
    obligations.set_sender(lambda recipient, state: None)
    try:

        def sign(entry_type):
            m = {"START": "authored", "STOP": "authored"}.get(entry_type, "performed")
            d = service.create_draft(
                site_id=SITE, logbook="watch_log", entry_type=entry_type, meaning=m,
                content={"title": entry_type.lower(), "fields": {}}, origin="human", for_principal=ME,
            )  # fmt: skip
            return _sign_draft(d, node_signer)

        sign("START")
        now["t"] = t0 + timedelta(minutes=10)
        check = sign("CHECK")
        due = obligations.evaluate(SITE, "watch_log", now=now["t"])[0]["due_at"]
        now["t"] = t0 + timedelta(minutes=38)
        ack(node_signer, check["attestation_id"])
        (state,) = obligations.evaluate(SITE, "watch_log", now=t0 + timedelta(minutes=45))
        assert state["due_at"] == due and state["state"] == "missed"
    finally:
        obligations.reset_sender()
        site_settings.unregister()


def _ack_flow(client, record_id):
    r = client.post(f"/api/v1/attest/demo_log/records/{record_id}/ack", json={})
    assert r.status_code == 201, r.text
    assert r.json()["kind"] == "ack" and r.json()["target"] == record_id
    pid = client.post(
        f"/api/v1/attest/drafts/{r.json()['draft_id']}/present", json={"modality": "screen"}
    ).json()["presentation_id"]
    client.post(
        f"/api/v1/attest/presentations/{pid}/respond", json={"answer": "sign", "via": "screen"}
    )
    g = client.post("/gate/grants", json={"presentation_id": pid, "console_id": "c1"})
    assert g.status_code == 200, g.text
    s = client.post(
        "/api/v1/attest/sign", json={"presentation_id": pid, "grant": g.json()["grant"]}
    )
    return s


def test_l3_the_http_route_acknowledges_lists_and_refuses(client, tmp_path):
    rec = sign_round(client)
    rid = rec["attestation_id"]
    url = f"/api/v1/attest/demo_log/records/{rid}/ack"
    # The signer's own entry.
    own = as_(client, OP1).post(url, json={})
    assert own.status_code == 422 and "own entry" in own.json()["detail"]
    listed = client.get("/api/v1/attest/demo_log/records?unacknowledged=1").json()["records"]
    assert [r["attestation_id"] for r in listed] == [rid]
    # Another person with a role acknowledges on a screen.
    (tmp_path / "attest" / "roles.toml").write_text(
        '[[assignment]]\nprincipal = "@op1:site-a"\nsite = "site-a"\nroles = ["operator"]\n'
        '[[assignment]]\nprincipal = "@op2:site-a"\nsite = "site-a"\nroles = ["operator"]\n'
    )
    as_(client, OP2)
    s = _ack_flow(client, rid)
    assert s.status_code == 201, s.text
    assert s.json()["record"]["kind"] == "ack"
    again = client.post(url, json={})
    assert again.status_code == 422 and "already acknowledged" in again.json()["detail"]
    assert client.get("/api/v1/attest/demo_log/records?unacknowledged=1").json()["records"] == []
    assert len(client.get("/api/v1/attest/demo_log/records").json()["records"]) == 2
    got = client.get(f"/api/v1/attest/demo_log/records/{rid}").json()
    assert [t["kind"] for t in got["thread"]] == ["statement", "ack"]
    missing = "/api/v1/attest/demo_log/records/00000000-0000-0000-0000-000000000000/ack"
    assert client.post(missing, json={}).json()["code"] == "not_found"
    other = f"/api/v1/attest/other_log/records/{rid}/ack"
    assert client.post(other, json={}).status_code == 404
    an_ack = s.json()["record"]["attestation_id"]
    r = client.post(f"/api/v1/attest/demo_log/records/{an_ack}/ack", json={})
    assert r.status_code == 422 and "cannot be acknowledged" in r.json()["detail"]
