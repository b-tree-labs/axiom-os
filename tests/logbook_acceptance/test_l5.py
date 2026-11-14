# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Acceptance tests for unit L5: seal a period, with summary, open items and chain head.

Ticket: docs/working/logbook-units/L5.md in the product repo that holds the plan, built against
`docs/working/design-note-l5-seal.md`. Rows A1 to A8 are Axiom's; A9 (`SealSheet`) is appkit's.
The fixture is `logbooks/seal_log.toml` beside this file: a run interval, a check a lead must
co-sign, a check obligation while a run is open, and a `[seal]` on the run.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text

pytest.importorskip("fastapi")

from axiom.extensions.builtins.attest import (  # noqa: E402  # noqa: E402
    cosign,
    grants,
    obligations,
    registry,
    service,
    site_settings,
    store,
)
from axiom.extensions.builtins.attest import roles as role_store  # noqa: E402
from axiom.extensions.builtins.attest.grants import (  # noqa: E402
    Authentication,
    Device,
    GrantRefused,
)
from axiom.extensions.builtins.attest.logbooks import LogbookError, parse_logbook  # noqa: E402
from axiom.extensions.builtins.attest.service import AttestRefused  # noqa: E402
from axiom.extensions.builtins.attest.skills import SPECS, bind_default  # noqa: E402
from axiom.extensions.builtins.attest.tests.test_api import OP1, OP2, as_  # noqa: E402
from axiom.extensions.builtins.attest.tests.test_signing import person  # noqa: E402
from axiom.infra.principal import PrincipalContext  # noqa: E402
from axiom.infra.skills import SkillContext  # noqa: E402

SITE = "site-a"
LOG = "seal_log"
MEMBER = "@op1:site-a"
LEAD = "@op2:site-a"
SEAL_LOG = Path(__file__).parent / "logbooks" / "seal_log.toml"
DESK = Device(device_id="desk-1", device_class="personal")
T0 = datetime(2026, 10, 1, 8, 0, tzinfo=UTC)


@pytest.fixture
def seal_log(attest_db, monkeypatch):
    registry.register_file(SEAL_LOG)
    cosign.set_sender(lambda recipient, info: None)
    obligations.set_sender(lambda recipient, state: None)
    now = {"t": T0}
    monkeypatch.setattr(service, "_now", lambda: now["t"])
    yield {"logbook": registry.get(LOG), "clock": now}
    cosign.reset_sender()
    obligations.reset_sender()
    site_settings.unregister()


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


def entry(signer, entry_type="NOTE", who=None, **fields):
    who = who or member()
    content = {
        "START": {"title": "Start", "fields": {}},
        "CHECK": {"title": "Check", "fields": {"ok": True}},
        "NOTE": {"title": "Note", "fields": {"text": "hello"}},
        "STOP": {"title": "Stop", "fields": {}},
    }[entry_type]
    content = {**content, "fields": {**content["fields"], **fields}}
    meaning = "performed" if entry_type == "CHECK" else "authored"
    d = service.create_draft(
        site_id=SITE, logbook=LOG, entry_type=entry_type, meaning=meaning, content=content,
        origin="human", for_principal=who.principal.handle,
    )  # fmt: skip
    return _sign_draft(d, signer, who)


def seal_draft(summary="All quiet.", boundary="run", who=None, **kw):
    who = who or lead()
    return service.create_seal_draft(
        site_id=SITE, logbook=LOG, boundary=boundary, summary=summary, origin="human",
        for_principal=who.principal.handle, **kw,
    )  # fmt: skip


def grant_for(presentation_id, who, signer, *, minutes_ago=1, device=DESK):
    auth = Authentication(
        principal=who.principal, auth_time=datetime.now(UTC) - timedelta(minutes=minutes_ago)
    )
    return grants.mint(presentation_id, auth=auth, device=device, signer=signer)


def seal_with_grant(signer, who=None, *, minutes_ago=1, **kw):
    who = who or lead()
    d = seal_draft(who=who, **kw)
    p = service.present(d, modality="screen")
    service.respond(p.presentation_id, principal=who.principal, answer="sign", via="screen")
    token = grant_for(p.presentation_id, who, signer, minutes_ago=minutes_ago)
    return service.sign(
        p.presentation_id, signatory=who, signer=signer, grant=token, grant_keys=_keys(signer)
    )


def a_run(signer, clock):
    """START, CHECK (co-signed), NOTE, STOP, each acknowledged by the lead: a closed run with
    nothing open."""
    start = entry(signer, "START")
    clock["t"] += timedelta(minutes=5)
    check = entry(signer, "CHECK")
    cd = service.create_cosign_draft(
        site_id=SITE, target=check["attestation_id"], origin="human", for_principal=LEAD
    )
    _sign_draft(cd, signer, lead())
    clock["t"] += timedelta(minutes=5)
    note = entry(signer, "NOTE")
    clock["t"] += timedelta(minutes=5)
    stop = entry(signer, "STOP")
    for rec in (start, check, note, stop):
        ad = service.create_ack_draft(
            site_id=SITE, target=rec["attestation_id"], origin="human", for_principal=LEAD
        )
        _sign_draft(ad, signer, lead())
    return start, check, note, stop


# -- acceptance rows ----------------------------------------------------------------------


@pytest.mark.acceptance("LB-L5-A1")
def test_l5_a1_a_seal_commits_to_the_chain_head_and_verifies(seal_log, node_signer):
    start, check, note, stop = a_run(node_signer, seal_log["clock"])
    assert service.verify_logbook(SITE, LOG, _keys(node_signer)).ok
    head = service.records(SITE, LOG)[-1]  # the lead's last acknowledgement
    result = seal_with_grant(node_signer)
    s = result.record
    assert s["kind"] == "seal" and s["meaning"] == "sealed" and s["entry_type"] is None
    # The period runs from the first unsealed record to the chain head at the time, so the
    # seal commits to the run's entries and every act on them.
    assert s["content"]["seal"]["covers"] == {
        "from_seq": start["seq"], "to_seq": head["seq"], "head_seq": head["seq"], "head_digest": head["digest"]
    }  # fmt: skip
    assert head["seq"] == stop["seq"] + 4
    assert s["content"]["seal"]["boundary"] == "run" and s["content"]["seal"]["open_items"] == []
    assert s["content"]["summary"] == "All quiet." and s["content"]["title"] == "Close out: run 1"
    assert s["target"] == head["attestation_id"] and s["target_digest"] == head["digest"]
    assert s["signer"]["principal"] == LEAD and s["signer"]["authority"]["roles"] == ["lead"]
    report = service.verify_logbook(SITE, LOG, _keys(node_signer))
    assert report.ok and report.head_digest == s["digest"]
    assert [b["kind"] for b in service.thread(head["attestation_id"])] == ["ack", "seal"]
    assert result.anchor and result.anchor["anchor_id"]


@pytest.mark.acceptance("LB-L5-A2")
def test_l5_a2_open_items_are_computed_and_signed(seal_log, node_signer, tmp_path):
    clock = seal_log["clock"]
    start = entry(node_signer, "START")
    clock["t"] += timedelta(minutes=10)
    check = entry(node_signer, "CHECK")  # nobody co-signs it, nobody acknowledges it
    clock["t"] += timedelta(minutes=40)  # the next check is missed
    obligations.tick(SITE, LOG, now=clock["t"], state_dir=tmp_path)
    open_draft = service.create_draft(
        site_id=SITE, logbook=LOG, entry_type="NOTE", meaning="authored",
        content={"title": "Pending note", "fields": {"text": "x"}}, origin="human", for_principal=MEMBER,
    )  # fmt: skip
    preview = service.seal_preview(SITE, LOG, boundary="run")
    kinds = sorted((i["kind"], i["ref"]) for i in preview["open_items"])
    assert kinds == sorted([
        ("pending_cosign", check["attestation_id"]),
        ("unacknowledged", start["attestation_id"]),
        ("unacknowledged", check["attestation_id"]),
        ("missed_obligation", "check_cadence:1"),
        ("open_draft", open_draft),
        ("open_interval", "run:1"),
    ])  # fmt: skip
    assert all(i["summary"] for i in preview["open_items"])
    assert (
        preview["covers"]["from_seq"] == start["seq"]
        and preview["covers"]["to_seq"] == check["seq"]
    )

    # The items are part of what is signed: a draft made now and one made after a co-signature
    # present different digests, and the first can no longer be presented as it was.
    d1 = seal_draft()
    p1 = service.present(d1, modality="screen")
    cd = service.create_cosign_draft(
        site_id=SITE, target=check["attestation_id"], origin="human", for_principal=LEAD
    )
    _sign_draft(cd, node_signer, lead())
    with pytest.raises(AttestRefused, match="changed since the draft"):
        service.present(d1, modality="screen")
    d2 = seal_draft()
    p2 = service.present(d2, modality="screen")
    assert p1.digest != p2.digest
    items2 = service.draft(d2)["content"]["seal"]["open_items"]
    assert ("pending_cosign", check["attestation_id"]) not in [
        (i["kind"], i["ref"]) for i in items2
    ]
    s = seal_with_grant(node_signer).record
    assert [i["kind"] for i in s["content"]["seal"]["open_items"]].count("open_interval") == 1
    assert "Open item" in p2.rendered


@pytest.mark.acceptance("LB-L5-A3")
def test_l5_a3_only_a_by_roles_holder_with_a_fresh_sign_in_seals(seal_log, node_signer):
    a_run(node_signer, seal_log["clock"])
    for who in (
        member(handle=LEAD, roles=("member",)),
        person(handle=LEAD, roles=("admin",)),
        person(handle=LEAD, roles=()),
    ):
        d = seal_draft(who=who)
        p = service.present(d, modality="screen")
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
    who = lead()
    d = seal_draft(who=who)
    p = service.present(d, modality="screen")
    service.respond(p.presentation_id, principal=who.principal, answer="sign", via="screen")
    with pytest.raises(GrantRefused) as exc:
        grant_for(p.presentation_id, who, node_signer, minutes_ago=6)
    assert exc.value.code == "reauth_required" and exc.value.max_age == 300
    with pytest.raises(AttestRefused, match="signing grant"):
        service.sign(p.presentation_id, signatory=who, signer=node_signer)
    token = grant_for(p.presentation_id, who, node_signer, minutes_ago=2)
    assert (
        service.sign(
            p.presentation_id,
            signatory=who,
            signer=node_signer,
            grant=token,
            grant_keys=_keys(node_signer),
        ).status
        == "signed"
    )
    with pytest.raises(LogbookError, match="administration"):
        parse_logbook({
            "logbook": {"id": "x_log", "version": "1"},
            "type": [{"id": "T", "meanings": ["authored"], "roles": ["r"]}],
            "seal": {"by_roles": ["admin"]},
        })  # fmt: skip


@pytest.mark.acceptance("LB-L5-A4")
def test_l5_a4_refusals_name_their_reason(seal_log, node_signer):
    clock = seal_log["clock"]
    _, _, _, stop = a_run(node_signer, clock)
    with pytest.raises(AttestRefused, match="summary"):
        seal_draft(summary="   ")
    with pytest.raises(AttestRefused, match=r"boundary.*run.*range") as exc:
        seal_draft(boundary="shift")
    assert "shift" in str(exc.value)
    with pytest.raises(AttestRefused, match="empty"):
        seal_draft(boundary="range", from_seq=3, to_seq=2)
    with pytest.raises(AttestRefused, match="head"):
        seal_draft(boundary="range", from_seq=1, to_seq=stop["seq"] + 5)
    with pytest.raises(AttestRefused, match="from_seq"):
        seal_draft(boundary="range")
    first = seal_with_grant(node_signer).record
    with pytest.raises(AttestRefused, match=f"already sealed.*#{first['seq']}"):
        seal_draft(boundary="range", from_seq=2, to_seq=3)
    with pytest.raises(AttestRefused, match="already sealed"):
        seal_draft()  # the latest run is the sealed one
    # A second run: a draft whose range moved cannot be presented without a new computation.
    clock["t"] += timedelta(minutes=1)
    entry(node_signer, "START")
    d = seal_draft()
    clock["t"] += timedelta(minutes=1)
    entry(node_signer, "NOTE")
    with pytest.raises(AttestRefused, match="changed since the draft"):
        service.present(d, modality="screen")
    fresh = seal_draft()
    assert service.present(fresh, modality="screen").digest
    with pytest.raises(AttestRefused, match="no record"):
        service.create_seal_draft(
            site_id="site-b",
            logbook=LOG,
            boundary="run",
            summary="x",
            origin="human",
            for_principal=LEAD,
        )


@pytest.mark.acceptance("LB-L5-A5")
def test_l5_a5_the_seal_is_anchored_and_a_rewrite_underneath_it_is_reported(seal_log, node_signer, tmp_path):
    start, check, note, stop = a_run(node_signer, seal_log["clock"])
    before = len(service.anchors(SITE))
    result = seal_with_grant(node_signer)
    s = result.record
    anchors = service.anchors(SITE)
    assert len(anchors) == before + 1 and anchors[-1]["anchor_id"] == result.anchor["anchor_id"]
    assert {"logbook": LOG, "seq": s["seq"], "digest": s["digest"]} in anchors[-1]["heads"]
    assert service.verify_site_anchor(result.anchor["anchor_id"], _keys(node_signer)) == (
        True,
        None,
    )
    # The attack an anchor exists for: whoever holds the node key rewrites a covered record and
    # re-signs every record after it, the seal included, so the chain still verifies.
    from axiom.attest.chain import seal as chain

    records = service.records(SITE, LOG)
    rewritten = []
    prev = note["prev_digest"]
    for rec in records:
        if rec["seq"] < note["seq"]:
            continue
        body = {
            k: v for k, v in rec.items() if k not in ("seq", "prev_digest", "digest", "node_sig")
        }
        if rec["seq"] == note["seq"]:
            body["content"] = {**rec["content"], "title": "Note (rewritten)"}
        forged = chain(body, seq=rec["seq"], prev_digest=prev, signer=node_signer)
        prev = forged["digest"]
        rewritten.append((rec["attestation_id"], forged))
    with store.session_scope() as db:
        db.execute(text("ALTER TABLE attest_records DISABLE TRIGGER attest_records_append_only"))
        for attestation_id, forged in rewritten:
            db.execute(
                text(
                    "UPDATE attest_records SET record = CAST(:rec AS jsonb), digest = :d WHERE attestation_id = :id"
                ),
                {"rec": json.dumps(forged), "d": forged["digest"], "id": attestation_id},
            )
        db.execute(text("ALTER TABLE attest_records ENABLE TRIGGER attest_records_append_only"))
        db.execute(
            text(
                "UPDATE attest_chain_heads SET digest = :d WHERE site_id = :site AND logbook = :logbook"
            ),
            {"d": prev, "site": SITE, "logbook": LOG},
        )
        db.commit()
    assert service.verify_logbook(SITE, LOG, _keys(node_signer)).ok, (
        "the forgery re-signed the chain"
    )
    ok, reason = service.verify_site_anchor(result.anchor["anchor_id"], _keys(node_signer))
    assert not ok and reason == f"head_rewritten:{LOG}"
    # And the verify skill says so beside the chain's own result.
    c = SkillContext(
        registry=bind_default(), state_dir=tmp_path, logger=logging.getLogger("test.l5")
    )
    from axiom.extensions.builtins.attest import signing

    signing.set_provider(lambda: node_signer)
    try:
        v = c.registry.invoke("attest.verify", {"site": SITE, "logbook": LOG}, c)
    finally:
        signing.reset_provider()
    assert not v.ok and v.value["ok"] is True  # the chain verifies; the anchor does not
    assert (
        v.value["anchor"]["ok"] is False and v.value["anchor"]["reason"] == f"head_rewritten:{LOG}"
    )


@pytest.mark.acceptance("LB-L5-A6")
def test_l5_a6_a_seal_locks_nothing(seal_log, node_signer):
    clock = seal_log["clock"]
    start, check, note, stop = a_run(node_signer, clock)
    due_before = obligations.evaluate(SITE, LOG, now=clock["t"])
    s = seal_with_grant(node_signer).record
    assert "interval" not in s
    assert obligations.evaluate(SITE, LOG, now=clock["t"]) == due_before
    # A supplement to a sealed entry, and a new run after the seal, are accepted.
    sd = service.create_supplement_draft(
        site_id=SITE, target=note["attestation_id"], reason="addition", content={"body": "later"},
        origin="human", for_principal=MEMBER,
    )  # fmt: skip
    sup = _sign_draft(sd, node_signer, member())
    assert sup["target"] == note["attestation_id"]
    clock["t"] += timedelta(minutes=1)
    start2 = entry(node_signer, "START")
    assert start2["interval"] == {"kind": "run", "number": 2, "event": "opened"}
    # The next seal starts right after the first (the supplement is part of the next period)
    # and never covers it.
    clock["t"] += timedelta(minutes=1)
    entry(node_signer, "STOP")
    second = seal_with_grant(node_signer).record
    covers = second["content"]["seal"]["covers"]
    assert covers["from_seq"] == sup["seq"] == s["seq"] + 1
    assert covers["from_seq"] <= start2["seq"] <= covers["to_seq"]
    assert service.verify_logbook(SITE, LOG, _keys(node_signer)).ok


@pytest.mark.acceptance("LB-L5-A7")
def test_l5_a7_seal_is_enforced_and_an_unresolvable_boundary_still_loads(seal_log):
    b = seal_log["logbook"]
    assert "seal" not in b.not_yet_enforced
    assert (b.seal.by_roles, b.seal.posture, b.seal.fresh_within_seconds, b.seal.boundary) == (
        ("lead",),
        "sso",
        300,
        "run",
    )
    loose = parse_logbook({
        "logbook": {"id": "x_log", "version": "1"},
        "type": [{"id": "T", "meanings": ["authored"], "roles": ["r"]}],
        "seal": {"by_roles": ["r"], "boundary": "shift"},
    })  # fmt: skip
    assert loose.seal.boundary == "shift" and loose.not_yet_enforced == ("seal.boundary",)
    assert (loose.seal.posture, loose.seal.fresh_within_seconds) == ("sso", None)
    ranged = parse_logbook({
        "logbook": {"id": "x_log", "version": "1"},
        "type": [{"id": "T", "meanings": ["authored"], "roles": ["r"]}],
        "seal": {"by_roles": ["r"], "boundary": "range"},
    })  # fmt: skip
    assert ranged.not_yet_enforced == ()
    with pytest.raises(LogbookError, match="unknown key"):
        parse_logbook({
            "logbook": {"id": "x_log", "version": "1"},
            "type": [{"id": "T", "meanings": ["authored"], "roles": ["r"]}],
            "seal": {"by_roles": ["r"], "boundry": "run"},
        })  # fmt: skip
    with pytest.raises(LogbookError, match="role"):
        parse_logbook({
            "logbook": {"id": "x_log", "version": "1"},
            "type": [{"id": "T", "meanings": ["authored"], "roles": ["r"]}],
            "seal": {"by_roles": []},
        })  # fmt: skip


def _ctx(state_dir, *, handle=LEAD, actor=None, surface="cli", answers=None, posture="sso"):
    replies = list(answers or [])
    return SkillContext(
        registry=bind_default(), state_dir=state_dir, logger=logging.getLogger("test.l5"),
        principal=PrincipalContext(handle=handle, posture=posture, assured=posture != "service"),
        actor=actor, surface=surface,
        user_prompt=(lambda _t: replies.pop(0)) if answers is not None else None,
    )  # fmt: skip


@pytest.mark.acceptance("LB-L5-A8")
def test_l5_a8_only_a_person_at_the_cli_seals_and_software_only_proposes(
    seal_log, node_signer, tmp_path, monkeypatch
):
    from axiom.extensions.builtins.attest import proposals, signing

    monkeypatch.setattr(proposals, "_agent_names", lambda: {"clerk"})
    by_name = {s.name: s for s in SPECS}
    assert by_name["attest.seal"].surfaces == ("cli",)
    assert set(by_name["attest.seal_propose"].surfaces) == {"mcp", "agent_tool"}
    assert by_name["attest.seal_propose"].side_effects is True
    assert set(by_name["attest.seal_preview"].surfaces) == {"cli", "mcp", "agent_tool"}
    assert by_name["attest.seal_preview"].side_effects is False
    # A sealable log whose seal needs no fresh sign-in, so the CLI may sign it.
    registry.register(parse_logbook({
        "logbook": {"id": "plain_log", "version": "1"},
        "interval": {"run": {"opens": ["START"], "closes": ["STOP"]}},
        "type": [
            {"id": "START", "meanings": ["authored"], "roles": ["member"], "requires_interval": "none_open"},
            {"id": "STOP", "meanings": ["authored"], "roles": ["member"], "requires_interval": "run"},
        ],
        "seal": {"by_roles": ["lead"], "boundary": "run"},
    }, source="test:plain"))  # fmt: skip
    for t in ("START", "STOP"):
        d = service.create_draft(site_id=SITE, logbook="plain_log", entry_type=t, meaning="authored",
                                 content={"title": t, "fields": {}}, origin="human", for_principal=MEMBER)  # fmt: skip
        _sign_draft(d, node_signer, member())
    role_store.grant(LEAD, SITE, "lead", by="@admin:site-a", state_dir=tmp_path)
    signing.set_provider(lambda: node_signer)
    try:
        params = {"site": SITE, "logbook": "plain_log", "boundary": "run", "summary": "Done."}
        c = _ctx(tmp_path)
        preview = c.registry.invoke(
            "attest.seal_preview", {"site": SITE, "logbook": "plain_log", "boundary": "run"}, c
        )
        assert (
            preview.ok
            and preview.value["covers"]["from_seq"] == 1
            and preview.value["covers"]["to_seq"] == 2
        )
        nobody = c.registry.invoke("attest.seal", params, c)
        assert not nobody.ok and "nothing was sealed" in nobody.errors[0]
        machine = _ctx(tmp_path, answers=["sign"], posture="service")
        refused = machine.registry.invoke("attest.seal", params, machine)
        assert not refused.ok and "posture" in refused.errors[0]
        # Software proposes a summary for a person; it never signs and cannot set what is covered.
        agent = _ctx(tmp_path, actor="@clerk:site-a", surface="mcp")
        spoofed = agent.registry.invoke(
            "attest.seal_propose",
            {**params, "for_principal": LEAD, "covers": {"from_seq": 1, "to_seq": 1}},
            agent,
        )
        assert not spoofed.ok and "not a parameter" in spoofed.errors[0]
        spoofed = agent.registry.invoke(
            "attest.seal_propose", {**params, "for_principal": LEAD, "open_items": []}, agent
        )
        assert not spoofed.ok
        proposed = agent.registry.invoke(
            "attest.seal_propose", {**params, "for_principal": LEAD}, agent
        )
        assert proposed.ok, proposed.errors
        assert proposed.value["origin"] == "agent:clerk" and proposed.value["kind"] == "seal"
        assert proposed.value["content"]["seal"]["covers"]["to_seq"] == 2
        assert len(service.records(SITE, "plain_log")) == 2
        c = _ctx(tmp_path, answers=["sign"])
        signed = c.registry.invoke("attest.seal", params, c)
        assert signed.ok, signed.errors
        assert signed.value["anchor_id"]
        assert service.record(signed.value["attestation_id"])["kind"] == "seal"
    finally:
        signing.reset_provider()


def test_l5_the_http_routes_preview_draft_and_report_the_anchor(client, tmp_path, seal_log):
    (tmp_path / "attest" / "roles.toml").write_text(
        '[[assignment]]\nprincipal = "@op1:site-a"\nsite = "site-a"\nroles = ["member"]\n'
        '[[assignment]]\nprincipal = "@op2:site-a"\nsite = "site-a"\nroles = ["lead"]\n'
    )

    def http_sign(c, body):
        d = c.post("/api/v1/attest/drafts", json=body)
        assert d.status_code == 201, d.text
        pid = c.post(
            f"/api/v1/attest/drafts/{d.json()['draft_id']}/present", json={"modality": "screen"}
        ).json()["presentation_id"]
        c.post(
            f"/api/v1/attest/presentations/{pid}/respond", json={"answer": "sign", "via": "screen"}
        )
        g = c.post("/gate/grants", json={"presentation_id": pid, "console_id": "c1"}).json()[
            "grant"
        ]
        r = c.post("/api/v1/attest/sign", json={"presentation_id": pid, "grant": g})
        assert r.status_code == 201, r.text
        return r.json()

    as_(client, OP1)
    declared = client.get(f"/api/v1/attest/logbooks/{LOG}").json()["seal"]
    assert declared == {
        "by_roles": ["lead"],
        "posture": "sso",
        "fresh_within_seconds": 300,
        "boundary": "run",
    }
    assert client.get("/api/v1/attest/logbooks/demo_log").json()["seal"] is None
    http_sign(
        client,
        {
            "logbook": LOG,
            "entry_type": "START",
            "meaning": "authored",
            "title": "Start",
            "fields": {},
        },
    )
    http_sign(
        client,
        {
            "logbook": LOG,
            "entry_type": "STOP",
            "meaning": "authored",
            "title": "Stop",
            "fields": {},
        },
    )
    pv = client.get(f"/api/v1/attest/{LOG}/seal/preview?boundary=run")
    assert pv.status_code == 200, pv.text
    items = pv.json()["open_items"]
    # Two unacknowledged entries, each named in words with its parts (LV-27).
    assert pv.json()["covers"]["to_seq"] == 2 and [i["kind"] for i in items] == ["unacknowledged", "unacknowledged"]
    assert [i["summary"] for i in items] == [f"#{i['seq']} {i['title']} is not yet acknowledged" for i in items]
    bad = client.get(f"/api/v1/attest/{LOG}/seal/preview?boundary=shift")
    assert bad.status_code == 422 and "run" in bad.json()["detail"]
    blank = client.post(f"/api/v1/attest/{LOG}/seal", json={"boundary": "run", "summary": " "})
    assert blank.status_code == 422
    as_(client, OP2)
    r = client.post(f"/api/v1/attest/{LOG}/seal", json={"boundary": "run", "summary": "Quiet."})
    assert r.status_code == 201, r.text
    draft = r.json()
    assert draft["kind"] == "seal" and draft["content"]["seal"]["covers"]["head_seq"] == 2
    pid = client.post(
        f"/api/v1/attest/drafts/{draft['draft_id']}/present", json={"modality": "screen"}
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
    assert s.json()["record"]["kind"] == "seal" and s.json()["anchor"]["anchor_id"]
    v = client.post(f"/api/v1/attest/{LOG}/verify").json()
    assert v["ok"] and v["checked"] == 3
    again = client.post(f"/api/v1/attest/{LOG}/seal", json={"boundary": "run", "summary": "Again."})
    assert again.status_code == 422 and "already sealed" in again.json()["detail"]
    unknown = client.get("/api/v1/attest/other_log/seal/preview?boundary=run")
    assert unknown.status_code == 404
