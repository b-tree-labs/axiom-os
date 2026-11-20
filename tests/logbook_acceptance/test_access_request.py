# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Asking for access to a log, or for the approver role (#1867; the owner's decision of 2026-11-16).

The rule, as the owner decided it: when a person can already know a log or an entry exists another
way (the log is listed for them, or they opened a link someone at the site sent them to an entry),
the node names it and offers "Ask for access" with the whole request path: the request, its
confirmation, a follow-up, and "previously refused" when it was. When they would have no other way
to know it exists, the node answers 404 and names nothing (LB-W1-A9 stands for that case). The
request path never reveals anything they could not already see.

- LB-NA-A1: a log that is not listed and that the person may not read is still absent everywhere,
  and asking for access to it answers 404.
- LB-NA-A2: an entry link (its id is a random UUID, so holding it means someone sent it) in a log
  the person may not read answers 403 ``no_access``: the log's name, who decides, the person's own
  request; never the entry's title, fields, number or signer. An id that is not there answers 404.
- LB-NA-A3: a log declared ``listed`` is in the person's list with its name and ``may_read`` false,
  and nothing else; its doors answer 403 ``no_access``.
- LB-NA-A4: Ask for access sends one request to the people who decide (``access_by``), says to whom
  and when, and a second ask returns the open one; Withdraw ends it; Remind is once a day.
- LB-NA-A5: the deciders see it in Needs you; Share grants the log's reader role and the person
  reads; Decline needs a reason, and the person then sees it as "previously refused" and may ask
  again.
- LB-NA-A6: ``listed`` and ``access_by`` are declared and validated; administration roles decide
  nothing (ADR-142 rule 8).
- LB-NA-A7: a site member who is not an approver sees the held actions read-only, with who can
  decide; they may ask for the approver role, the approvers answer it, and their decision door
  still refuses them until then.
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest

pytest.importorskip("fastapi")

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from axiom.extensions.builtins.attest import registry  # noqa: E402
from axiom.extensions.builtins.attest.logbooks import load_logbook  # noqa: E402
from axiom.extensions.builtins.attest.tests.test_api import (  # noqa: E402
    OP1,
    OP2,
    SITE,
    as_,
    sign_round,
)
from axiom.webauth.users import User  # noqa: E402

from .test_x3 import gate, held  # noqa: E402,F401

# ruff: noqa: F811  (pytest fixtures imported from the X3 tests)

KP = User(user_id="kp", email="kp@example.org", name="Kay Keeper", roles=("user",), site=SITE,
          attributes={"idp": "entra"})  # fmt: skip

KEPT_LOG = """
[logbook]
id = "kept_log"
handle = "kept"
version = "1"
display = "Kept Log"
readers = ["auditor"]
access_by = ["keeper"]
assurance = { posture = "sso" }

[[type]]
id = "ROUND_CHECK"
meanings = ["performed"]
roles = ["operator"]
fields = [
  { id = "reading", type = "text", observe = true, required = true },
  { id = "walkdown", type = "attest_checkbox", label = "Walkdown performed", required = true },
  { id = "note", type = "text" },
]
"""


def _grant(state_dir, assignments):
    """Replace the site's role file: ``{principal: [roles]}`` at the test site."""
    (state_dir / "attest").mkdir(exist_ok=True)
    rows = []
    for who, theirs in assignments.items():
        quoted = ", ".join(f'"{r}"' for r in theirs)
        rows.append(f'[[assignment]]\nprincipal = "{who}"\nsite = "{SITE}"\nroles = [{quoted}]\n')
    (state_dir / "attest" / "roles.toml").write_text("".join(rows))


ROLES = {"@op1:site-a": ["operator"], "@kp:site-a": ["keeper"]}


@pytest.fixture
def requests_db(tmp_path):
    """The access-request table on SQLite (the authz tables, as the Library's tests bind them)."""
    from axiom.extensions.builtins.authz.db_models import Base
    from axiom.governance.access import store

    engine = create_engine(f"sqlite:///{tmp_path / 'authz.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    @contextmanager
    def provider():
        s = factory()
        try:
            yield s
        finally:
            s.close()

    store.set_session_provider(provider)
    yield factory
    store.set_session_provider(None)


@pytest.fixture(autouse=True)
def names():
    """The site's accounts, which give each person's name."""
    from axiom.webauth.users import InMemoryUserStore, reset_user_store_for_tests, set_user_store

    set_user_store(InMemoryUserStore([OP1, OP2, KP]))
    yield
    reset_user_store_for_tests()


def _log(tmp_path, text=KEPT_LOG, name="kept_log.toml"):
    path = tmp_path / name
    path.write_text(text)
    registry.register_file(path)
    return path


@pytest.fixture
def kept(client, tmp_path, requests_db):
    _log(tmp_path)
    _grant(tmp_path, ROLES)
    rec = sign_round(client, logbook="kept_log")
    return client, tmp_path, rec


@pytest.fixture
def listed(client, tmp_path, requests_db):
    _log(tmp_path, KEPT_LOG.replace('access_by = ["keeper"]', 'access_by = ["keeper"]\nlisted = true'))
    _grant(tmp_path, ROLES)
    rec = sign_round(client, logbook="kept_log")
    return client, tmp_path, rec


ASK = "/api/v1/attest/kept_log/access-request"


@pytest.mark.acceptance("LB-NA-A1")
def test_lb_na_a1_a_log_nobody_could_know_of_is_still_absent_and_asking_answers_404(kept):
    c, _state, _rec = kept
    as_(c, OP2)
    ids = {b["id"] for b in c.get("/api/v1/attest/logbooks").json()["logbooks"]}
    assert "kept_log" not in ids
    for path in ("/api/v1/attest/logbooks/kept_log", "/api/v1/attest/kept_log/records"):
        r = c.get(path)
        assert (r.status_code, r.json()["code"]) == (404, "not_found"), path
    assert c.post(ASK, json={}).status_code == 404
    assert c.get(ASK).status_code == 404
    # naming an entry that is not in the log proves nothing
    r = c.post(ASK, json={"record": "00000000-0000-4000-8000-000000000000"})
    assert r.status_code == 404


@pytest.mark.acceptance("LB-NA-A2")
def test_lb_na_a2_an_entry_link_names_the_log_and_who_decides_and_nothing_of_the_entry(kept):
    c, _state, rec = kept
    r = as_(c, OP2).get(f"/api/v1/attest/kept_log/records/{rec['attestation_id']}")
    assert r.status_code == 403, r.text
    body = r.json()
    assert body["code"] == "no_access"
    assert body["logbook"] == {"id": "kept_log", "display": "Kept Log"}
    assert [d["name"] for d in body["deciders"]] == ["Kay Keeper"]
    assert body["request"] == {"open": None, "previously_refused": None}
    text = r.text
    for secret in ("Round check", "walkdown", "reading", "@op1", "Op One", '"seq"', rec["digest"]):
        assert secret not in text, secret
    missing = c.get("/api/v1/attest/kept_log/records/00000000-0000-4000-8000-000000000000")
    assert (missing.status_code, missing.json()["code"]) == (404, "not_found")


@pytest.mark.acceptance("LB-NA-A3")
def test_lb_na_a3_a_listed_log_is_named_in_the_list_and_its_doors_say_no_access(listed):
    c, _state, _rec = listed
    as_(c, OP2)
    books = {b["id"]: b for b in c.get("/api/v1/attest/logbooks").json()["logbooks"]}
    assert books["kept_log"] == {"id": "kept_log", "display": "Kept Log", "may_read": False}
    assert books["demo_log"]["may_read"] is True
    for path in ("/api/v1/attest/logbooks/kept_log", "/api/v1/attest/kept_log/records"):
        r = c.get(path)
        assert (r.status_code, r.json()["code"]) == (403, "no_access"), path
        assert r.json()["logbook"]["display"] == "Kept Log"
        assert "Round check" not in r.text


@pytest.mark.acceptance("LB-NA-A4")
def test_lb_na_a4_ask_withdraw_and_remind_once_a_day(kept):
    c, _state, rec = kept
    as_(c, OP2)
    link = {"record": rec["attestation_id"], "note": "  I review the round  "}
    first = c.post(ASK, json=link)
    assert first.status_code == 201, first.text
    req = first.json()
    assert req["state"] == "open" and req["note"] == "I review the round"
    assert [d["name"] for d in req["sent_to"]] == ["Kay Keeper"] and req["created_at"]
    again = c.post(ASK, json=link)
    assert again.status_code == 200 and again.json()["id"] == req["id"] and again.json()["already_open"]
    mine = c.get(ASK, params={"record": rec["attestation_id"]}).json()
    assert mine["open"]["id"] == req["id"]
    remind = c.post(f"/api/v1/attest/access-requests/{req['id']}/remind")
    assert remind.status_code == 409 and "one reminder a day" in remind.json()["detail"]
    # only the person who asked may withdraw it
    other = as_(c, OP1).post(f"/api/v1/attest/access-requests/{req['id']}/withdraw")
    assert other.status_code == 404
    gone = as_(c, OP2).post(f"/api/v1/attest/access-requests/{req['id']}/withdraw")
    assert gone.status_code == 200 and gone.json()["state"] == "withdrawn"
    assert c.get(ASK, params={"record": rec["attestation_id"]}).json()["open"] is None


@pytest.mark.acceptance("LB-NA-A5")
def test_lb_na_a5_the_keeper_sees_it_in_needs_you_and_shares_or_declines(kept, monkeypatch):
    c, state, rec = kept
    from axiom.extensions.builtins.attest import queue

    monkeypatch.setattr(queue, "_state_dir", lambda: state)
    as_(c, OP2)
    req = c.post(ASK, json={"record": rec["attestation_id"]}).json()
    items = [i for i in queue.for_person("@kp:site-a") if i["kind"] == "access"]
    assert [i["title"] for i in items] == ["Op Two asks to read Kept Log"]
    assert items[0]["section"] == "due" and "@" not in items[0]["title"]
    assert [i for i in queue.for_person("@op1:site-a") if i["kind"] == "access"] == []
    # only a decider answers
    assert c.post(f"/api/v1/attest/access-requests/{req['id']}/answer", json={"approve": True}).status_code == 404
    k = as_(c, KP)
    assert [r["id"] for r in k.get("/api/v1/attest/access-requests").json()["requests"]] == [req["id"]]
    no_reason = k.post(f"/api/v1/attest/access-requests/{req['id']}/answer", json={"approve": False})
    assert no_reason.status_code == 422
    declined = k.post(f"/api/v1/attest/access-requests/{req['id']}/answer",
                      json={"approve": False, "reason": "for the round crew only"})  # fmt: skip
    assert declined.status_code == 200 and declined.json()["state"] == "declined"
    as_(c, OP2)
    seen = c.get(f"/api/v1/attest/kept_log/records/{rec['attestation_id']}").json()["request"]
    assert seen["open"] is None
    refused = seen["previously_refused"]
    assert refused["by"] == "Kay Keeper" and refused["reason"] == "for the round crew only" and refused["at"]
    # ask again, and this time the keeper shares
    second = c.post(ASK, json={"record": rec["attestation_id"]}).json()
    shared = as_(c, KP).post(f"/api/v1/attest/access-requests/{second['id']}/answer", json={"approve": True})
    assert shared.status_code == 200 and shared.json()["granted_role"] == "auditor"
    r = as_(c, OP2).get(f"/api/v1/attest/kept_log/records/{rec['attestation_id']}")
    assert r.status_code == 200 and r.json()["record"]["attestation_id"] == rec["attestation_id"]


@pytest.mark.acceptance("LB-NA-A6")
def test_lb_na_a6_listed_and_access_by_are_declared_and_validated(tmp_path):
    book = load_logbook(_write(tmp_path, KEPT_LOG))
    assert book.access_by == ("keeper",) and book.listed is False
    assert "logbook.access_by" not in book.not_yet_enforced
    with pytest.raises(Exception, match="administration"):
        load_logbook(_write(tmp_path, KEPT_LOG.replace('access_by = ["keeper"]', 'access_by = ["admin"]')))
    with pytest.raises(Exception, match="listed"):
        load_logbook(_write(tmp_path, KEPT_LOG.replace('access_by = ["keeper"]', 'listed = "yes"')))


def _write(tmp_path, text):
    path = tmp_path / "check.toml"
    path.write_text(text)
    return path


@pytest.mark.acceptance("LB-NA-A7")
def test_lb_na_a7_a_member_sees_held_actions_read_only_and_asks_for_the_approver_role(
    client, gate, requests_db, tmp_path
):
    action = held(gate)
    c = as_(client, OP1)  # an operator at the site, not an approver
    body = c.get("/api/v1/attest/approvals/pending").json()
    assert [a["action_id"] for a in body["pending"]] == [action.action_id]
    assert body["pending"][0]["may_decide"] is False
    access = body["approver_access"]
    assert [d["name"] for d in access["deciders"]] == ["Op Two"]
    assert access["request"] == {"open": None, "previously_refused": None}
    refused = c.post(f"/api/v1/attest/approvals/{action.action_id}/decision", json={"decision": "approved"})
    assert refused.status_code == 403 and refused.json()["code"] == "not_approver"
    asked = c.post("/api/v1/attest/approvals/access-request", json={"note": "covering nights"})
    assert asked.status_code == 201 and [d["name"] for d in asked.json()["sent_to"]] == ["Op Two"]
    again = c.get("/api/v1/attest/approvals/pending").json()["approver_access"]["request"]
    assert again["open"]["id"] == asked.json()["id"]
    a = as_(client, OP2)
    assert a.get("/api/v1/attest/approvals/pending").json()["pending"][0]["may_decide"] is True
    ok = a.post(f"/api/v1/attest/access-requests/{asked.json()['id']}/answer", json={"approve": True})
    assert ok.status_code == 200 and ok.json()["granted_role"] == "approver"
    after = as_(client, OP1).get("/api/v1/attest/approvals/pending").json()
    assert after["pending"][0]["may_decide"] is True
    # a person with no role at the site sees no held action at all
    assert as_(client, KP).get("/api/v1/attest/approvals/pending").json()["pending"] == []
