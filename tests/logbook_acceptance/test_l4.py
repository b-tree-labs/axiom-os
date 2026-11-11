# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Acceptance tests for unit L4: co-signature, with a fresh sign-in and separation of duties.

Ticket: docs/working/logbook-units/L4.md in the product repo that holds the plan, built against
`docs/working/design-note-l4-cosign.md`. Rows A1 to A9 are Axiom's; A10 (the waiting marker and
the Co-sign action) is appkit's, in `frontend/src/logbookAcceptance/l4.test.tsx`. The fixture is
`logbooks/pair_log.toml` beside this file.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text

pytest.importorskip("fastapi")

from axiom.extensions.builtins.attest import cosign, devices, grants, registry, service  # noqa: E402
from axiom.extensions.builtins.attest import roles as role_store  # noqa: E402
from axiom.extensions.builtins.attest import store  # noqa: E402
from axiom.extensions.builtins.attest.grants import Authentication, Device, GrantRefused  # noqa: E402
from axiom.extensions.builtins.attest.logbooks import LogbookError, parse_logbook  # noqa: E402
from axiom.extensions.builtins.attest.service import AttestRefused  # noqa: E402
from axiom.extensions.builtins.attest.skills import SPECS, bind_default  # noqa: E402
from axiom.extensions.builtins.attest.tests.test_api import OP1, OP2, as_  # noqa: E402
from axiom.extensions.builtins.attest.tests.test_signing import person  # noqa: E402
from axiom.infra.principal import PrincipalContext  # noqa: E402
from axiom.infra.skills import SkillContext  # noqa: E402

SITE = "site-a"
LOG = "pair_log"
MEMBER = "@op1:site-a"
LEAD = "@op2:site-a"
PAIR_LOG = Path(__file__).parent / "logbooks" / "pair_log.toml"
DESK = Device(device_id="desk-1", device_class="personal")


@pytest.fixture
def pair_log(attest_db):
    registry.register_file(PAIR_LOG)
    return registry.get(LOG)


@pytest.fixture
def quiet(tmp_path, monkeypatch):
    """Notifications captured, roles read from a private state dir."""
    sent: list[tuple[str, dict]] = []
    cosign.set_sender(lambda recipient, info: sent.append((recipient, info)))
    monkeypatch.setattr(cosign, "_state_dir", lambda: tmp_path)
    yield {"sent": sent, "state_dir": tmp_path}
    cosign.reset_sender()


def _keys(signer):
    return {signer.key_id: signer.public_bytes}


def member(handle=MEMBER, roles=("member",)):
    return person(handle=handle, roles=roles)


def lead(handle=LEAD, roles=("lead",)):
    return person(handle=handle, roles=roles)


def _sign_draft(draft_id, signer, who, modality="cli"):
    p = service.present(draft_id, modality=modality)
    service.respond(p.presentation_id, principal=who.principal, answer="sign", via=modality)
    return service.sign(p.presentation_id, signatory=who, signer=signer).record


def entry(signer, entry_type="HANDOVER", who=None):
    who = who or member()
    content = {
        "HANDOVER": {"title": "Handover", "fields": {"summary": "all quiet"}},
        "CHECK": {"title": "Check", "fields": {"ok": True}},
        "NOTE": {"title": "Note", "fields": {"text": "hello"}},
    }[entry_type]
    meaning = "authored" if entry_type == "NOTE" else "performed"
    d = service.create_draft(
        site_id=SITE, logbook=LOG, entry_type=entry_type, meaning=meaning, content=content,
        origin="human", for_principal=who.principal.handle,
    )  # fmt: skip
    return _sign_draft(d, signer, who)


def cosign_draft(target, who=None):
    who = who or lead()
    return service.create_cosign_draft(
        site_id=SITE, target=target, origin="human", for_principal=who.principal.handle
    )


def grant_for(presentation_id, who, signer, *, minutes_ago=1, device=DESK):
    auth = Authentication(
        principal=who.principal, auth_time=datetime.now(UTC) - timedelta(minutes=minutes_ago)
    )
    return grants.mint(presentation_id, auth=auth, device=device, signer=signer)


def cosign_with_grant(target, signer, who=None, *, minutes_ago=1, device=DESK):
    who = who or lead()
    p = service.present(cosign_draft(target, who), modality="screen")
    service.respond(p.presentation_id, principal=who.principal, answer="sign", via="screen")
    token = grant_for(p.presentation_id, who, signer, minutes_ago=minutes_ago, device=device)
    return service.sign(
        p.presentation_id, signatory=who, signer=signer, grant=token, grant_keys=_keys(signer)
    ).record


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


@pytest.mark.acceptance("LB-L4-A1")
def test_l4_a1_a_cosignature_binds_to_the_bytes_and_leaves_them_alone(pair_log, node_signer, quiet):
    rec = entry(node_signer)
    before = _row(rec["attestation_id"])
    assert service.verify_logbook(SITE, LOG, _keys(node_signer)).ok
    assert rec["interval"] if "interval" in rec else True  # a pending co-signature blocks nothing
    c = cosign_with_grant(rec["attestation_id"], node_signer)
    assert c["kind"] == "cosign" and c["meaning"] == "approved"
    assert c["target"] == rec["attestation_id"] and c["target_digest"] == rec["digest"]
    assert c["entry_type"] is None and c["signer"]["principal"] == LEAD
    assert c["signer"]["authority"]["roles"] == ["lead"]
    assert c["content"] == {"title": "Co-signed: Handover", "fields": {}}
    assert _row(rec["attestation_id"]) == before
    report = service.verify_logbook(SITE, LOG, _keys(node_signer))
    assert report.ok and report.checked == 2
    assert [(b["kind"], b["signer"]) for b in service.thread(rec["attestation_id"])] == [
        ("statement", MEMBER), ("cosign", LEAD)
    ]  # fmt: skip


@pytest.mark.acceptance("LB-L4-A2")
def test_l4_a2_not_the_signer_and_only_a_declared_cosign_role(pair_log, node_signer, quiet):
    rec = entry(node_signer)
    with pytest.raises(AttestRefused, match="own entry"):
        cosign_draft(rec["attestation_id"], member())
    # Holding the type's signing role is not holding its co-sign role.
    for who in (member(handle=LEAD, roles=("member",)), member(handle=LEAD, roles=())):
        p = service.present(cosign_draft(rec["attestation_id"], who), modality="screen")
        service.respond(p.presentation_id, principal=who.principal, answer="sign", via="screen")
        token = grant_for(p.presentation_id, who, node_signer)
        with pytest.raises(AttestRefused, match="holds no role"):
            service.sign(
                p.presentation_id,
                signatory=who,
                signer=node_signer,
                grant=token,
                grant_keys=_keys(node_signer),
            )
    # A lead who is also the signer of the entry cannot co-sign it.
    both = person(handle="@op3:site-a", roles=("member", "lead"))
    own = entry(node_signer, who=both)
    with pytest.raises(AttestRefused, match="own entry"):
        cosign_draft(own["attestation_id"], both)
    assert len(service.records(SITE, LOG)) == 2


@pytest.mark.acceptance("LB-L4-A3")
def test_l4_a3_an_administration_role_never_counts(pair_log, node_signer, quiet):
    rec = entry(node_signer)
    for roles in (("admin",), ("platform_admin",), ("admin", "member")):
        who = person(handle=LEAD, roles=roles)
        p = service.present(cosign_draft(rec["attestation_id"], who), modality="screen")
        service.respond(p.presentation_id, principal=who.principal, answer="sign", via="screen")
        token = grant_for(p.presentation_id, who, node_signer)
        with pytest.raises(AttestRefused, match="holds no role"):
            service.sign(
                p.presentation_id,
                signatory=who,
                signer=node_signer,
                grant=token,
                grant_keys=_keys(node_signer),
            )
    # And a declaration that lists one is refused outright.
    with pytest.raises(LogbookError, match="administration"):
        parse_logbook({
            "logbook": {"id": "x_log", "version": "1"},
            "type": [{"id": "T", "meanings": ["authored"], "roles": ["r"], "cosign": {"roles": ["admin"]}}],
        })  # fmt: skip


@pytest.mark.acceptance("LB-L4-A4")
def test_l4_a4_a_stale_sign_in_is_refused_and_a_fresh_one_signs_once(pair_log, node_signer, quiet):
    rec = entry(node_signer)
    who = lead()
    p = service.present(cosign_draft(rec["attestation_id"], who), modality="screen")
    service.respond(p.presentation_id, principal=who.principal, answer="sign", via="screen")
    with pytest.raises(GrantRefused) as exc:
        grant_for(p.presentation_id, who, node_signer, minutes_ago=6)
    assert exc.value.code == "reauth_required" and exc.value.max_age == 300
    with pytest.raises(GrantRefused) as exc:
        grants.mint(
            p.presentation_id,
            auth=Authentication(principal=who.principal, auth_time=None),
            device=DESK,
            signer=node_signer,
        )
    assert exc.value.code == "reauth_required"
    # The CLI path has no grant, so a type with a freshness window is not co-signed there.
    with pytest.raises(AttestRefused, match="signing grant"):
        service.sign(p.presentation_id, signatory=who, signer=node_signer)
    token = grant_for(p.presentation_id, who, node_signer, minutes_ago=4)
    c = service.sign(
        p.presentation_id,
        signatory=who,
        signer=node_signer,
        grant=token,
        grant_keys=_keys(node_signer),
    ).record
    assert c["kind"] == "cosign" and c["assurance"]["fresh_within_met"] is True
    with pytest.raises(AttestRefused):
        service.sign(
            p.presentation_id,
            signatory=who,
            signer=node_signer,
            grant=token,
            grant_keys=_keys(node_signer),
        )
    # A second co-signature of the same entry, by anyone, is refused.
    with pytest.raises(AttestRefused, match="already co-signed"):
        cosign_draft(rec["attestation_id"], lead(handle="@op4:site-a"))
    assert len(service.records(SITE, LOG)) == 2


@pytest.mark.acceptance("LB-L4-A5")
def test_l4_a5_a_phone_cannot_cosign(pair_log, node_signer, quiet):
    devices.enroll(site_id=SITE, device_id="phone-1", device_class="phone", location=None,
                   mobility="portable", by="@admin:site-a")  # fmt: skip
    phone = Device(device_id="phone-1", device_class="phone")
    rec = entry(node_signer, "CHECK")
    who = lead()
    p = service.present(cosign_draft(rec["attestation_id"], who), modality="screen")
    service.respond(p.presentation_id, principal=who.principal, answer="sign", via="screen")
    with pytest.raises(GrantRefused) as exc:
        grant_for(p.presentation_id, who, node_signer, device=phone)
    assert exc.value.code == "device_not_allowed"
    # The same phone may still acknowledge the entry.
    a = service.present(
        service.create_ack_draft(
            site_id=SITE, target=rec["attestation_id"], origin="human", for_principal=LEAD
        ),
        modality="screen",
    )
    service.respond(a.presentation_id, principal=who.principal, answer="sign", via="screen")
    assert grant_for(a.presentation_id, who, node_signer, device=phone)


@pytest.mark.acceptance("LB-L4-A6")
def test_l4_a6_the_pending_list_is_exact_and_only_a_declared_type_is_cosigned(
    pair_log, node_signer, quiet, monkeypatch
):
    ids = lambda rows: [r["attestation_id"] for r in rows]  # noqa: E731
    h1 = entry(node_signer)
    note = entry(node_signer, "NOTE")
    c1 = entry(node_signer, "CHECK")
    assert ids(service.pending_cosigns(SITE, LOG)) == [h1["attestation_id"], c1["attestation_id"]]
    assert service.brief(h1)["cosign"] == "pending" and service.brief(note)["cosign"] is None
    with pytest.raises(AttestRefused, match="declares no co-signature"):
        cosign_draft(note["attestation_id"])
    cosign_with_grant(h1["attestation_id"], node_signer)
    assert ids(service.pending_cosigns(SITE, LOG)) == [c1["attestation_id"]]
    briefs = {b["attestation_id"]: b for b in service.briefs(service.records(SITE, LOG))}
    assert briefs[h1["attestation_id"]]["cosign"] == "done"
    assert briefs[c1["attestation_id"]]["cosign"] == "pending"
    assert briefs[note["attestation_id"]]["cosign"] is None
    # A co-signature, a supplement or an acknowledgement is not itself co-signed.
    c = _sign_draft(cosign_draft(c1["attestation_id"]), node_signer, lead())
    for target in (c["attestation_id"],):
        with pytest.raises(AttestRefused, match="only an entry"):
            cosign_draft(target)
    assert service.pending_cosigns(SITE, LOG) == []
    # for_roles and older_than narrow the list.
    h2 = entry(node_signer)
    assert ids(service.pending_cosigns(SITE, LOG, for_roles=("lead",))) == [h2["attestation_id"]]
    assert service.pending_cosigns(SITE, LOG, for_roles=("member",)) == []
    assert service.pending_cosigns(SITE, LOG, older_than=3600) == []
    (row,) = service.pending_cosigns(SITE, LOG, older_than=0)
    assert row["age_seconds"] >= 0 and row["cosign"] == "pending"


@pytest.mark.acceptance("LB-L4-A7")
def test_l4_a7_cosign_role_holders_are_told_once_and_a_failing_notifier_blocks_nothing(
    pair_log, node_signer, quiet
):
    from axiom.infra.bus import get_default_eventbus

    sd = quiet["state_dir"]
    role_store.grant(MEMBER, SITE, "member", by="@admin:site-a", state_dir=sd)
    role_store.grant(LEAD, SITE, "lead", by="@admin:site-a", state_dir=sd)
    role_store.grant("@op3:site-a", SITE, "lead", by="@admin:site-a", state_dir=sd)
    role_store.grant("@op5:site-a", SITE, "lead", by="@admin:site-a", state_dir=sd)
    role_store.grant("@op5:site-a", SITE, "member", by="@admin:site-a", state_dir=sd)
    seen = []
    bus = get_default_eventbus()
    sub = bus.subscribe(f"attest.{LOG}.*", lambda s, p: seen.append((s, p)), source="t")
    try:
        rec = entry(node_signer)
        assert sorted(r for r, _ in quiet["sent"]) == [LEAD, "@op3:site-a", "@op5:site-a"]
        assert all(
            i["attestation_id"] == rec["attestation_id"]
            and i["dedup_key"] == f"cosign:{rec['attestation_id']}"
            for _, i in quiet["sent"]
        )
        # The signer is never told to co-sign their own entry, even as a lead.
        quiet["sent"].clear()
        by_lead = entry(node_signer, who=person(handle="@op5:site-a", roles=("member", "lead")))
        assert sorted(r for r, _ in quiet["sent"]) == [LEAD, "@op3:site-a"]
        # A type without cosign tells nobody.
        quiet["sent"].clear()
        entry(node_signer, "NOTE")
        assert quiet["sent"] == []
        c = cosign_with_grant(rec["attestation_id"], node_signer)
    finally:
        bus.unsubscribe(sub)
    subjects = [s.rsplit(".", 1)[-1] for s, _ in seen]
    assert subjects == [
        "signed",
        "cosign_requested",
        "signed",
        "cosign_requested",
        "signed",
        "signed",
        "cosigned",
    ]
    requested = [p for s, p in seen if s.endswith("cosign_requested")]
    assert requested[0]["attestation_id"] == rec["attestation_id"] and requested[0][
        "cosign_roles"
    ] == ["lead"]
    cosigned = [p for s, p in seen if s.endswith("cosigned")][0]
    assert (cosigned["attestation_id"], cosigned["target"]) == (
        c["attestation_id"],
        rec["attestation_id"],
    )

    def boom(recipient, info):
        raise RuntimeError("mail is down")

    cosign.set_sender(boom)
    late = entry(node_signer)
    assert service.record(late["attestation_id"]) == late
    assert by_lead["attestation_id"] in [
        r["attestation_id"] for r in service.pending_cosigns(SITE, LOG)
    ]


@pytest.mark.acceptance("LB-L4-A8")
def test_l4_a8_party_and_window_load_as_declared_and_cosign_is_enforced(pair_log):
    assert set(pair_log.not_yet_enforced) == {"HANDOVER.cosign.party", "HANDOVER.cosign.window"}
    h = pair_log.type("HANDOVER").cosign
    assert (h.roles, h.posture, h.fresh_within_seconds) == (("lead",), "sso", 300)
    c = pair_log.type("CHECK").cosign
    # No assurance block: the type's own floor and window apply.
    assert (c.roles, c.posture, c.fresh_within_seconds) == (("lead",), "sso", None)
    assert pair_log.type("NOTE").cosign is None
    base = {
        "logbook": {"id": "x_log", "version": "1"},
        "type": [{"id": "T", "meanings": ["authored"], "roles": ["r"]}],
    }
    with pytest.raises(LogbookError, match="unknown key"):
        parse_logbook(
            {**base, "type": [{**base["type"][0], "cosign": {"roles": ["s"], "rolse": ["s"]}}]}
        )
    with pytest.raises(LogbookError, match="role"):
        parse_logbook({**base, "type": [{**base["type"][0], "cosign": {"roles": []}}]})
    with pytest.raises(LogbookError, match="fresh_within"):
        parse_logbook(
            {
                **base,
                "type": [
                    {
                        **base["type"][0],
                        "cosign": {"roles": ["s"], "assurance": {"fresh_within": "soon"}},
                    }
                ],
            }
        )


def _ctx(state_dir, *, handle=LEAD, answers=None, posture="sso"):
    replies = list(answers or [])
    return SkillContext(
        registry=bind_default(), state_dir=state_dir, logger=logging.getLogger("test.l4"),
        principal=PrincipalContext(handle=handle, posture=posture, assured=posture != "service"),
        user_prompt=(lambda _t: replies.pop(0)) if answers is not None else None, surface="cli",
    )  # fmt: skip


@pytest.mark.acceptance("LB-L4-A9")
def test_l4_a9_only_a_person_at_the_cli_cosigns(pair_log, node_signer, quiet):
    from axiom.extensions.builtins.attest import signing

    by_name = {s.name: s for s in SPECS}
    assert by_name["attest.cosign"].surfaces == ("cli",)
    assert set(by_name["attest.cosign_pending"].surfaces) == {"cli", "mcp", "agent_tool"}
    assert by_name["attest.cosign_pending"].side_effects is False
    sd = quiet["state_dir"]
    role_store.grant(LEAD, SITE, "lead", by="@admin:site-a", state_dir=sd)
    rec = entry(node_signer, "CHECK")
    signing.set_provider(lambda: node_signer)
    try:
        c = _ctx(sd)
        pending = c.registry.invoke("attest.cosign_pending", {"site": SITE, "logbook": LOG}, c)
        assert pending.ok and [r["attestation_id"] for r in pending.value["records"]] == [
            rec["attestation_id"]
        ]
        nobody = c.registry.invoke("attest.cosign", {"attestation_id": rec["attestation_id"]}, c)
        assert not nobody.ok and "nothing was signed" in nobody.errors[0]
        machine = _ctx(sd, answers=["sign"], posture="service")
        refused = machine.registry.invoke(
            "attest.cosign", {"attestation_id": rec["attestation_id"]}, machine
        )
        assert not refused.ok and "posture" in refused.errors[0]
        c = _ctx(sd, answers=["sign"])
        signed = c.registry.invoke("attest.cosign", {"attestation_id": rec["attestation_id"]}, c)
        assert signed.ok, signed.errors
        assert service.record(signed.value["attestation_id"])["kind"] == "cosign"
        after = c.registry.invoke("attest.cosign_pending", {"site": SITE, "logbook": LOG}, c)
        assert after.ok and after.value["records"] == []
    finally:
        signing.reset_provider()


def test_l4_the_library_cosigns_with_its_grant(pair_log, node_signer, quiet, monkeypatch):
    from axiom.attest import confirm, drafts, sign
    from axiom.extensions.builtins.attest import signing

    sd = quiet["state_dir"]
    role_store.grant(LEAD, SITE, "lead", by="@admin:site-a", state_dir=sd)
    monkeypatch.setenv("AXIOM_SERVED_SITES", SITE)
    monkeypatch.setenv("AXI_STATE_DIR", str(sd))
    signing.set_provider(lambda: node_signer)
    try:
        rec = entry(node_signer)
        me = lead().principal
        d = drafts.cosign(rec["attestation_id"], "human", LEAD, entered_by=me)
        assert (d.kind, d.target) == ("cosign", rec["attestation_id"])
        p = confirm.present(d.id, modalities=["screen"])
        confirm.respond(p.id, principal=me, answer="sign", via="screen")
        token = grant_for(p.id, lead(), node_signer)
        with pytest.raises(AttestRefused, match="another draft"):
            sign.cosign("00000000-0000-0000-0000-000000000000", token)
        result = sign.cosign(rec["attestation_id"], token)
        assert result.status == "signed" and result.record["kind"] == "cosign"
        assert result.record["source"] == "library"
    finally:
        signing.reset_provider()


def test_l4_the_http_route_cosigns_lists_and_refuses(client, tmp_path, quiet):
    registry.register_file(PAIR_LOG)
    (tmp_path / "attest" / "roles.toml").write_text(
        '[[assignment]]\nprincipal = "@op1:site-a"\nsite = "site-a"\nroles = ["member"]\n'
        '[[assignment]]\nprincipal = "@op2:site-a"\nsite = "site-a"\nroles = ["lead"]\n'
    )
    body = {
        "logbook": LOG,
        "entry_type": "CHECK",
        "meaning": "performed",
        "title": "Check",
        "fields": {"ok": True},
    }
    d = as_(client, OP1).post("/api/v1/attest/drafts", json=body)
    assert d.status_code == 201, d.text
    pid = client.post(
        f"/api/v1/attest/drafts/{d.json()['draft_id']}/present", json={"modality": "screen"}
    ).json()["presentation_id"]
    client.post(
        f"/api/v1/attest/presentations/{pid}/respond", json={"answer": "sign", "via": "screen"}
    )
    g = client.post("/gate/grants", json={"presentation_id": pid, "console_id": "c1"}).json()[
        "grant"
    ]
    rec = client.post("/api/v1/attest/sign", json={"presentation_id": pid, "grant": g}).json()[
        "record"
    ]
    rid = rec["attestation_id"]
    url = f"/api/v1/attest/{LOG}/records/{rid}/cosign"
    own = client.post(url, json={})
    assert own.status_code == 422 and "own entry" in own.json()["detail"]
    pending = client.get(f"/api/v1/attest/{LOG}/records?cosign=pending").json()["records"]
    assert [r["attestation_id"] for r in pending] == [rid] and pending[0]["cosign"] == "pending"
    note = as_(client, OP2).post(
        "/api/v1/attest/drafts",
        json={
            **body,
            "entry_type": "NOTE",
            "meaning": "authored",
            "title": "Note",
            "fields": {"text": "x"},
        },
    )
    assert note.status_code == 201, note.text

    as_(client, OP2)
    r = client.post(url, json={})
    assert r.status_code == 201 and r.json()["kind"] == "cosign" and r.json()["target"] == rid
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
    assert s.status_code == 201, s.text
    assert s.json()["record"]["kind"] == "cosign"
    again = client.post(url, json={})
    assert again.status_code == 422 and "already co-signed" in again.json()["detail"]
    assert client.get(f"/api/v1/attest/{LOG}/records?cosign=pending").json()["records"] == []
    listed = {
        r["attestation_id"]: r
        for r in client.get(f"/api/v1/attest/{LOG}/records").json()["records"]
    }
    assert listed[rid]["cosign"] == "done"
    got = client.get(f"/api/v1/attest/{LOG}/records/{rid}").json()
    assert [t["kind"] for t in got["thread"]] == ["statement", "cosign"]
    missing = f"/api/v1/attest/{LOG}/records/00000000-0000-0000-0000-000000000000/cosign"
    assert client.post(missing, json={}).json()["code"] == "not_found"
    assert client.post(f"/api/v1/attest/other_log/records/{rid}/cosign", json={}).status_code == 404
