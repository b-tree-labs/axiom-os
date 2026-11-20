# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Acceptance tests for unit L2: supplement a signed entry.

Ticket: docs/working/logbook-units/L2.md in the product repo that holds the plan, built against
`docs/working/design-note-l2-supplement.md`. Rows A1 to A7 are Axiom's; A8 and A9 (the entry
detail and the Supplement action) are appkit's, in `frontend/src/logbookAcceptance/l2.test.tsx`.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

pytest.importorskip("fastapi")

from axiom.extensions.builtins.attest import (  # noqa: E402
    grants,
    obligations,
    proposals,
    registry,
    service,
    site_settings,
    store,
)
from axiom.extensions.builtins.attest import roles as role_store  # noqa: E402
from axiom.extensions.builtins.attest.logbooks import parse_logbook  # noqa: E402
from axiom.extensions.builtins.attest.service import AttestRefused  # noqa: E402
from axiom.extensions.builtins.attest.skills import SPECS, bind_default  # noqa: E402
from axiom.extensions.builtins.attest.tests.test_api import OP1, OP2, ROUND, as_  # noqa: E402
from axiom.extensions.builtins.attest.tests.test_signing import person  # noqa: E402
from axiom.infra.principal import PrincipalContext  # noqa: E402
from axiom.infra.skills import SkillContext  # noqa: E402

SITE = "site-a"
ME = "@op1:site-a"


def _keys(signer):
    return {signer.key_id: signer.public_bytes}


def _sign_draft(draft_id, signer, who=None):
    who = who or person()
    p = service.present(draft_id)
    service.respond(p.presentation_id, principal=who.principal, answer="sign", via="cli")
    return service.sign(p.presentation_id, signatory=who, signer=signer).record


def original(signer, reading="ok"):
    d = service.create_draft(
        site_id=SITE, logbook="demo_log", entry_type="ROUND_CHECK", meaning="performed",
        content={"title": "Round check", "fields": {"reading": reading, "walkdown": True}},
        origin="human", for_principal=ME,
    )  # fmt: skip
    return _sign_draft(d, signer)


def supplement(signer, target, reason="correction", content=None, who=None):
    d = service.create_supplement_draft(
        site_id=SITE, target=target, reason=reason,
        content=content if content is not None else {"fields": {"reading": "fixed"}},
        origin="human", for_principal=(who or person()).principal.handle,
    )  # fmt: skip
    return _sign_draft(d, signer, who)


def _row(attestation_id):
    # Through the extension's own session, which sets the attest search_path on its connection; a
    # raw pooled connection may carry another schema's path under xdist.
    with store.session_scope() as s:
        return s.execute(
            text(
                "SELECT record::text AS record, digest, prev_digest, seq, kind, target, entry_type, "
                "meaning, signer FROM attest_records WHERE attestation_id = :id"
            ),
            {"id": attestation_id},
        ).one()


# -- acceptance rows ----------------------------------------------------------------------


@pytest.mark.acceptance("LB-L2-A1")
def test_l2_a1_the_original_is_byte_identical_after_any_number_of_supplements(
    attest_db, node_signer
):
    rec = original(node_signer)
    before = _row(rec["attestation_id"])
    for reason in ("correction", "clarification", "addition", "correction"):
        supplement(node_signer, rec["attestation_id"], reason,
                   {"body": f"a {reason}", "fields": {"note": reason}})  # fmt: skip
    assert _row(rec["attestation_id"]) == before
    assert service.record(rec["attestation_id"]) == rec


@pytest.mark.acceptance("LB-L2-A2")
def test_l2_a2_a_supplement_points_at_the_original_and_its_digest(attest_db, node_signer):
    rec = original(node_signer)
    sup = supplement(node_signer, rec["attestation_id"])
    assert sup["kind"] == "supplement"
    assert sup["target"] == rec["attestation_id"]
    assert sup["target_digest"] == rec["digest"]
    assert sup["entry_type"] is None
    assert sup["seq"] == rec["seq"] + 1 and sup["prev_digest"] == rec["digest"]
    row = _row(sup["attestation_id"])
    assert (row.kind, row.target, row.entry_type) == ("supplement", sup["target"], None)
    assert json.loads(row.record)["target"] == row.target


@pytest.mark.acceptance("LB-L2-A3")
def test_l2_a3_the_reason_is_signed_and_closed(attest_db, node_signer):
    rec = original(node_signer)
    meanings = {}
    for reason in ("correction", "clarification", "addition"):
        sup = supplement(node_signer, rec["attestation_id"], reason, {"body": reason})
        assert sup["reason"] == reason
        meanings[reason] = sup["meaning"]
    assert meanings == {
        "correction": "corrected", "clarification": "authored", "addition": "authored"
    }  # fmt: skip
    for bad in ("retraction", "Correction", "", None):
        with pytest.raises(AttestRefused, match="reason"):
            service.create_supplement_draft(
                site_id=SITE, target=rec["attestation_id"], reason=bad, content={"body": "x"},
                origin="human", for_principal=ME,
            )  # fmt: skip


@pytest.mark.acceptance("LB-L2-A4")
def test_l2_a4_the_chain_verifies_before_and_after_and_standalone(
    attest_db, bound_signer, tmp_path
):
    signer = bound_signer
    rec = original(signer)
    report = service.verify_logbook(SITE, "demo_log", _keys(signer))
    assert report.ok and report.checked == 1
    sup = supplement(signer, rec["attestation_id"])
    supplement(signer, sup["attestation_id"], "clarification", {"body": "of the correction"})
    original(signer, reading="later")
    report = service.verify_logbook(SITE, "demo_log", _keys(signer))
    assert report.ok and report.checked == 4, report

    c = SkillContext(registry=bind_default(), state_dir=tmp_path,
                     logger=logging.getLogger("test.l2"))  # fmt: skip
    out = tmp_path / "package"
    r = c.registry.invoke("attest.export", {"site": SITE, "logbook": "demo_log", "out": out}, c)
    assert r.ok, r.errors
    run = subprocess.run(
        [sys.executable, "-I", "verify.py", "records.jsonl", "--keys", "keys.json"],
        cwd=out, capture_output=True, text=True, check=False,
    )  # fmt: skip
    assert run.returncode == 0, run.stdout + run.stderr


@pytest.fixture
def known_callers(monkeypatch):
    monkeypatch.setattr(proposals, "_agent_names", lambda: {"clerk"})
    monkeypatch.setattr(proposals, "_installed_extensions", lambda: set())


def _ctx(state_dir, *, actor=None, surface="mcp", answers=None):
    replies = list(answers or [])
    return SkillContext(
        registry=bind_default(), state_dir=state_dir, logger=logging.getLogger("test.l2"),
        principal=PrincipalContext(handle=ME, posture="sso", assured=True),
        actor=actor, surface=surface,
        user_prompt=(lambda _t: replies.pop(0)) if answers is not None else None,
    )  # fmt: skip


@pytest.mark.acceptance("LB-L2-A5")
def test_l2_a5_every_must_refuse_is_refused_with_a_reason(
    attest_db, node_signer, tmp_path, known_callers
):
    rec = original(node_signer)
    target = rec["attestation_id"]

    def refused(match, **over):
        kw = {"site_id": SITE, "target": target, "reason": "correction",
              "content": {"body": "x"}, "origin": "human", "for_principal": ME, **over}  # fmt: skip
        with pytest.raises(AttestRefused, match=match):
            service.create_supplement_draft(**kw)

    # Software never signs: the signing skill is on no agent surface, a proposal is only a
    # draft, and a principal without a signing posture is refused at sign.
    by_name = {s.name: s for s in SPECS}
    assert by_name["attest.supplement"].surfaces == ("cli",)
    assert set(by_name["attest.supplement_propose"].surfaces) == {"mcp", "agent_tool"}
    role_store.grant(ME, SITE, "operator", by="@admin:site-a", state_dir=tmp_path)
    proposed = _ctx(tmp_path, actor="@clerk:site-a").registry.invoke(
        "attest.supplement_propose",
        {"attestation_id": target, "reason": "addition", "body": "an agent noticed"},
        _ctx(tmp_path, actor="@clerk:site-a"),
    )
    assert proposed.ok, proposed.errors
    assert proposed.value["origin"] == "agent:clerk" and proposed.value["status"] == "open"
    assert proposed.value["for_principal"] == ME and proposed.value["kind"] == "supplement"
    assert len(service.records(SITE, "demo_log")) == 1
    d = proposed.value["draft_id"]
    p = service.present(d)
    machine = person(posture="service")
    service.respond(p.presentation_id, principal=machine.principal, answer="sign", via="cli")
    with pytest.raises(AttestRefused, match="posture"):
        service.sign(p.presentation_id, signatory=machine, signer=node_signer)

    # A target that does not exist, is in another logbook or site, or is of another kind.
    refused("no record", target="00000000-0000-0000-0000-000000000000")
    refused("no record", site_id="site-b")
    refused("no record", logbook="other_log")
    with store.session_scope() as s:
        s.execute(
            text(
                "INSERT INTO attest_records (attestation_id, site_id, logbook, seq, digest, "
                "prev_digest, kind, meaning, entry_type, signer, occurred_at, recorded_at, record) "
                "VALUES ('11111111-1111-1111-1111-111111111111', :site, 'demo_log_other', 1, "
                "repeat('a', 64), repeat('0', 64), 'ack', 'acknowledged', NULL, :me, now(), now(), "
                "CAST(:rec AS jsonb))"
            ),
            {"site": SITE, "me": ME, "rec": json.dumps({"kind": "ack"})},
        )
        s.commit()
    refused("cannot be supplemented", target="11111111-1111-1111-1111-111111111111")

    # A missing or unknown reason, and empty content.
    refused("reason", reason=None)
    refused("reason", reason="retraction")
    refused("body or at least one field", content={"title": "only a title"})
    refused("body or at least one field", content={"fields": {}})
    refused("unknown field", content={"fields": {"no_such_field": "1"}})

    # Software may not offer an observed value.
    refused("observe", origin="agent:clerk", content={"fields": {"reading": "9"}})

    # A signer with no role, or with only an administration role.
    for who in (person(roles=()), person(roles=("admin",))):
        d = service.create_supplement_draft(
            site_id=SITE, target=target, reason="correction", content={"body": "x"},
            origin="human", for_principal=ME,
        )  # fmt: skip
        with pytest.raises(AttestRefused, match="holds no role"):
            _sign_draft(d, node_signer, who)

    # No path updates or deletes a record: the append-only triggers are untouched.
    for statement in (
        "UPDATE attest_records SET meaning = 'corrected' WHERE attestation_id = :id",
        "DELETE FROM attest_records WHERE attestation_id = :id",
    ):
        with pytest.raises(Exception, match="append"), store.session_scope() as s:
            s.execute(text(statement), {"id": target})
            s.commit()
    assert service.record(target) == rec


def _grant_flow(client, record_id, reason="correction", fields=None):
    r = client.post(f"/api/v1/attest/demo_log/records/{record_id}/supplement",
                    json={"reason": reason, "fields": fields or {"reading": "fixed"}})  # fmt: skip
    assert r.status_code == 201, r.text
    pid = client.post(f"/api/v1/attest/drafts/{r.json()['draft_id']}/present",
                      json={"modality": "screen"}).json()["presentation_id"]  # fmt: skip
    client.post(f"/api/v1/attest/presentations/{pid}/respond",
                json={"answer": "sign", "via": "screen"})  # fmt: skip
    g = client.post("/gate/grants", json={"presentation_id": pid, "console_id": "c1"})
    assert g.status_code == 200, g.text
    return pid, g.json()["grant"]


@pytest.mark.acceptance("LB-L2-A5")
def test_l2_a5_grants_are_single_use_short_lived_and_bound_to_one_digest(client, monkeypatch):
    from axiom.extensions.builtins.attest.tests.test_api import sign_round

    rec = sign_round(client)
    rid = rec["attestation_id"]
    pid, grant = _grant_flow(client, rid)
    ok = client.post("/api/v1/attest/sign", json={"presentation_id": pid, "grant": grant})
    assert ok.status_code == 201, ok.text
    again = client.post("/api/v1/attest/sign", json={"presentation_id": pid, "grant": grant})
    assert again.status_code == 403 and again.json()["detail"]

    pid, grant = _grant_flow(client, rid)
    later = datetime.now(UTC) + timedelta(hours=1)
    with monkeypatch.context() as m:
        m.setattr(grants, "_now", lambda: later)
        expired = client.post("/api/v1/attest/sign", json={"presentation_id": pid, "grant": grant})
    assert expired.status_code == 403 and "expired" in expired.json()["detail"]

    # A grant for the first digest does not sign the corrected one.
    pid, grant = _grant_flow(client, rid)
    nxt = client.post(f"/api/v1/attest/presentations/{pid}/respond",
                      json={"answer": "ask", "via": "screen",
                            "correction": {"reading": "fixed again"}}).json()  # fmt: skip
    pid2 = nxt["next_presentation"]["presentation_id"]
    client.post(f"/api/v1/attest/presentations/{pid2}/respond",
                json={"answer": "sign", "via": "screen"})  # fmt: skip
    other = client.post("/api/v1/attest/sign", json={"presentation_id": pid2, "grant": grant})
    assert other.status_code == 403 and "not for this presentation" in other.json()["detail"]


WATCH = {
    "logbook": {"id": "watch_log", "version": "1"},
    "interval": {"run": {"opens": ["START"], "closes": ["STOP"]}},
    "type": [
        {"id": "START", "meanings": ["authored"], "roles": ["operator"], "requires_interval": "none_open"},
        {"id": "CHECK", "meanings": ["performed"], "roles": ["operator"], "requires_interval": "run"},
        {"id": "STOP", "meanings": ["authored"], "roles": ["operator"], "requires_interval": "run"},
    ],
    "obligation": [
        {"id": "check_cadence", "type": "CHECK", "every": {"default": 30},
         "warn_before": {"default": 5}, "while": "interval.run.open", "notify": ["operator"],
         "required": True},
    ],
}  # fmt: skip


@pytest.mark.acceptance("LB-L2-A6")
def test_l2_a6_a_supplement_never_meets_an_obligation_or_moves_an_interval(
    attest_db, node_signer, monkeypatch, tmp_path
):
    t0 = datetime(2026, 10, 1, 8, 0, tzinfo=UTC)
    now = {"t": t0}
    monkeypatch.setattr(service, "_now", lambda: now["t"])
    registry.register(parse_logbook(WATCH, source="test:watch"))
    obligations.set_sender(lambda recipient, state: None)

    def sign(entry_type):
        m = {"START": "authored", "STOP": "authored"}.get(entry_type, "performed")
        d = service.create_draft(
            site_id=SITE, logbook="watch_log", entry_type=entry_type, meaning=m,
            content={"title": entry_type.lower(), "fields": {}}, origin="human", for_principal=ME,
        )  # fmt: skip
        return _sign_draft(d, node_signer)

    def supplement_of(rec, body):
        d = service.create_supplement_draft(
            site_id=SITE, target=rec["attestation_id"], reason="correction",
            content={"body": body}, origin="human", for_principal=ME,
        )  # fmt: skip
        return _sign_draft(d, node_signer)

    def intervals():
        with store.session_scope() as s:
            return s.execute(text("SELECT * FROM attest_intervals ORDER BY number")).all()

    try:
        start = sign("START")
        now["t"] = t0 + timedelta(minutes=10)
        check = sign("CHECK")
        due = obligations.evaluate(SITE, "watch_log", now=now["t"])[0]["due_at"]
        now["t"] = t0 + timedelta(minutes=35)
        before = intervals()
        sup = supplement_of(check, "the reading was entered on the wrong line")
        assert "interval" not in sup and sup["entry_type"] is None
        assert intervals() == before
        (state,) = obligations.evaluate(SITE, "watch_log", now=t0 + timedelta(minutes=45))
        assert state["due_at"] == due and state["state"] == "missed"

        now["t"] = t0 + timedelta(minutes=50)
        sign("STOP")
        closed = intervals()
        # After the run has ended a correction is still possible, and opens nothing.
        now["t"] = t0 + timedelta(minutes=60)
        late = supplement_of(check, "corrected after the run")
        late_start = supplement_of(start, "and the start time too")
        assert "interval" not in late and "interval" not in late_start
        assert intervals() == closed
        assert service.verify_logbook(SITE, "watch_log", _keys(node_signer)).ok
    finally:
        obligations.reset_sender()
        site_settings.unregister()


@pytest.mark.acceptance("LB-L2-A7")
def test_l2_a7_the_record_returns_its_thread_and_the_stream_delivers_it(client):
    from axiom.extensions.builtins.attest.tests.test_api import sign_round
    from axiom.infra.bus import get_default_eventbus

    rec = sign_round(client)
    rid = rec["attestation_id"]
    seen: list[tuple[str, dict]] = []
    bus = get_default_eventbus()
    sub = bus.subscribe("attest.demo_log.*", lambda s, p: seen.append((s, p)), source="t")
    try:
        sups = []
        for reason in ("correction", "addition"):
            pid, grant = _grant_flow(client, rid, reason, {"note": reason})
            s = client.post("/api/v1/attest/sign", json={"presentation_id": pid, "grant": grant})
            assert s.status_code == 201, s.text
            sups.append(s.json()["record"])
    finally:
        bus.unsubscribe(sub)
    sign_round(client)  # an unrelated later entry is not in the thread

    got = client.get(f"/api/v1/attest/demo_log/records/{rid}").json()
    assert got["record"] == rec
    thread = got["thread"]
    assert [t["attestation_id"] for t in thread] == [rid, *(s["attestation_id"] for s in sups)]
    assert [t["seq"] for t in thread] == sorted(t["seq"] for t in thread)
    assert [t["kind"] for t in thread] == ["statement", "supplement", "supplement"]
    assert [t["target"] for t in thread] == [None, rid, rid]

    subjects = [s for s, _ in seen if not s.endswith("draft_created")]
    assert subjects == ["attest.demo_log.signed", "attest.demo_log.supplemented"] * 2
    supplemented = [p for s, p in seen if s.endswith(".supplemented")]
    assert [(p["target"], p["reason"]) for p in supplemented] == [
        (rid, "correction"), (rid, "addition")
    ]  # fmt: skip

    stream = client.get("/api/v1/attest/demo_log/stream?last_event_id=0&once=1")
    events = [
        json.loads(line[6:]) for line in stream.text.splitlines() if line.startswith("data: ")
    ]
    delivered = [e for e in events if e.get("kind") == "supplement"]
    assert [e["attestation_id"] for e in delivered] == [s["attestation_id"] for s in sups]


def test_l2_the_http_route_refuses_with_the_right_codes(client):
    from axiom.extensions.builtins.attest.tests.test_api import sign_round

    rec = sign_round(client)
    url = f"/api/v1/attest/demo_log/records/{rec['attestation_id']}/supplement"
    assert client.post(url, json={"body": "x"}).status_code == 422
    assert client.post(url, json={"reason": "nope", "body": "x"}).status_code == 422
    assert client.post(url, json={"reason": "correction"}).status_code == 422
    missing = "/api/v1/attest/demo_log/records/00000000-0000-0000-0000-000000000000/supplement"
    assert (
        client.post(missing, json={"reason": "correction", "body": "x"}).json()["code"]
        == "not_found"
    )
    other = f"/api/v1/attest/other_log/records/{rec['attestation_id']}/supplement"
    assert client.post(other, json={"reason": "correction", "body": "x"}).status_code == 404
    as_(client, OP2)
    d = client.post(url, json={"reason": "correction", "body": "x"})
    # OP2 may draft one, as anyone may write a draft; signing it needs a role (A5).
    assert d.status_code == 201 and d.json()["for_principal"] == "@op2:site-a"
    as_(client, OP1)
    assert ROUND["logbook"] == "demo_log"


@pytest.mark.acceptance("LB-L2-A4")
def test_l2_the_library_signs_a_supplement_with_its_grant(
    attest_db, bound_signer, roles, monkeypatch
):
    """`axiom.attest.sign.supplement(target, reason, content, grant)` keeps F1's signature: the
    grant names one presentation, so the call is the last step after drafts.supplement, present
    and respond (design note L2, section 8)."""
    from axiom.attest import confirm, drafts, sign

    monkeypatch.setenv("AXIOM_SERVED_SITES", SITE)
    monkeypatch.setenv("AXI_STATE_DIR", str(roles))
    me = PrincipalContext(handle=ME, posture="sso", assured=True)
    rec = original(bound_signer)
    content = {"body": "the reading was misread", "fields": {"reading": "fixed"}}

    def granted(draft):
        p = confirm.present(draft.id, modalities=["screen"])
        confirm.respond(p.id, principal=me, answer="sign", via="screen")
        return grants.mint(
            p.id, auth=grants.Authentication(principal=me, auth_time=datetime.now(UTC)),
            device=grants.Device(device_id="laptop", device_class="personal"), signer=bound_signer,
        )  # fmt: skip

    d = drafts.supplement(rec["attestation_id"], "correction", content, "human", ME, entered_by=me)
    assert (d.kind, d.target, d.reason) == ("supplement", rec["attestation_id"], "correction")
    grant = granted(d)
    for wrong in (
        ("00000000-0000-0000-0000-000000000000", "correction", content),
        (rec["attestation_id"], "addition", content),
        (rec["attestation_id"], "correction", {"body": "something else"}),
    ):
        with pytest.raises(AttestRefused, match="another draft"):
            sign.supplement(*wrong, grant)
    result = sign.supplement(rec["attestation_id"], "correction", content, grant)
    sup = result.record
    assert result.status == "signed" and sup["source"] == "library"
    assert (sup["target"], sup["reason"], sup["signer"]["principal"]) == (
        rec["attestation_id"], "correction", ME
    )  # fmt: skip
    with pytest.raises(AttestRefused):
        sign.supplement(rec["attestation_id"], "correction", content, grant)
    with pytest.raises(AttestRefused, match="entered_by"):
        drafts.supplement(rec["attestation_id"], "correction", content, "human", ME)
