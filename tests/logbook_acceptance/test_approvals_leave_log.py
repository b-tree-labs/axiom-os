# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Approvals leave the Log (appkit design ``proposed-2026-11-18/approvals``, approved 2026-11-18).

Decisions stay signed, chained records in the ``approvals`` log; only where they appear changes:

- LB-AL-A1: a log declared ``shown_in_log = false`` (the approvals log) is not listed by ``GET
  /logbooks``, with no special case on its id; ``attest.logbook_list`` still lists it, marked.
- LB-AL-A2: a pending approval waits in Needs you ("To approve") for an approver, linking to its
  card, and drops out once decided; nobody else is pushed it.
- LB-AL-A3: ``GET /decisions/{action_id}`` says the decision in words with the record one step
  away and its verification, to anyone with a role at the site (Q3); a pending action is 409.
- LB-AL-A4: the journal presents a decision as kind ``decided``, in the act's words, linking to
  that read; the Timeline's ``kind`` filter selects such rows.
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")

from axiom.extensions.builtins.attest import approvals, queue, registry  # noqa: E402
from axiom.extensions.builtins.attest.logbooks import LogbookError, load_logbook  # noqa: E402
from axiom.extensions.builtins.attest.skills.logbook import list_logbooks  # noqa: E402
from axiom.extensions.builtins.attest.tests.test_api import OP1, OP2, SITE, as_  # noqa: E402
from axiom.extensions.builtins.journal import rows as journal_rows  # noqa: E402
from axiom.extensions.builtins.journal.service import RowFilter  # noqa: E402
from axiom.infra.orchestrator.actions import Action  # noqa: E402
from axiom.webauth.users import User  # noqa: E402

from .test_j1 import bus, journal_db, live  # noqa: E402,F401
from .test_x3 import gate, present_decision, sign_decision  # noqa: E402,F401

# ruff: noqa: F811  (pytest fixtures imported from the J1 and X3 tests)

TITLE = "Archive last quarter's reports"
STRANGER = User(user_id="op9", email="op9@example.org", name="Op Nine", roles=("user",), site=SITE,
                attributes={"idp": "entra"})  # fmt: skip


def _held(g) -> Action:
    action = Action(name="reports.archive", params={"older_than": "90d"}, reason="cannot be undone",
                    title=TITLE, description="Old reports move to cold storage.", consequence="loss",
                    requested_by="@report-helper:agents")  # fmt: skip
    return g.submit(action)


@pytest.mark.acceptance("LB-AL-A1")
def test_lb_al_a1_a_log_not_shown_in_the_log_is_not_listed_and_maintainers_still_see_it(client, gate, tmp_path):
    ids = {b["id"] for b in as_(client, OP2).get("/api/v1/attest/logbooks").json()["logbooks"]}
    assert "approvals" not in ids
    assert registry.get("approvals").shown_in_log is False
    listed = {b["id"]: b for b in list_logbooks({}).value["logbooks"]}
    assert listed["approvals"]["shown_in_log"] is False
    # Generic: any log may say so, and the value is a boolean.
    bad = tmp_path / "bad.toml"
    bad.write_text(approvals.LOGBOOK_FILE.read_text(encoding="utf-8").replace("shown_in_log = false", 'shown_in_log = "no"'))
    with pytest.raises(LogbookError, match="shown_in_log is true or false"):
        load_logbook(bad)
    # Reading the log itself is unchanged.
    assert as_(client, OP2).get("/api/v1/attest/logbooks/approvals").status_code == 200


@pytest.mark.acceptance("LB-AL-A2")
def test_lb_al_a2_a_pending_approval_waits_in_needs_you_for_an_approver_only(client, gate, node_signer, tmp_path,
                                                                          monkeypatch):
    monkeypatch.setattr(queue, "_state_dir", lambda: tmp_path)
    action = _held(gate)
    mine = [i for i in queue.for_person("@op2:site-a") if i["section"] == "approve"]
    assert [(i["title"], i["source"], i["href"]) for i in mine] == [
        (TITLE, "report-helper asks", f"#/control?decide={action.action_id}")
    ]
    assert [i for i in queue.for_person("@op1:site-a") if i["section"] == "approve"] == []
    sign_decision(present_decision(action, approving=True), node_signer)
    assert [i for i in queue.for_person("@op2:site-a") if i["section"] == "approve"] == []


@pytest.mark.acceptance("LB-AL-A3")
def test_lb_al_a3_the_decision_in_words_for_anyone_who_can_see_the_thing(client, gate, node_signer):
    action = _held(gate)
    path = f"/api/v1/attest/decisions/{action.action_id}"
    r = as_(client, OP1).get(path)
    assert (r.status_code, r.json()["code"]) == (409, "pending")
    rec = sign_decision(present_decision(action, approving=True), node_signer).record
    for who in (OP1, OP2):  # an operator and the approver alike (Q3)
        got = as_(client, who).get(path)
        assert got.status_code == 200, got.text
        d = got.json()
        assert (d["decision"], d["title"], d["decided_by"]) == ("approved", TITLE, "@op2:site-a")
        assert d["decided_by_name"] and d["decided_at"]
        assert d["record"]["attestation_id"] == rec["attestation_id"]
        assert (d["record"]["seq"], d["record"]["capability"], d["record"]["digest"]) == (
            rec["seq"], "reports.archive", rec["digest"])  # fmt: skip
        assert d["verified"]["ok"] is True and d["verified"]["checked_at"]
    # A person with no role at the site cannot tell a decision exists.
    r = as_(client, STRANGER).get(path)
    assert r.status_code == 404
    assert as_(client, OP2).get("/api/v1/attest/decisions/no-such-action").status_code == 404


@pytest.mark.acceptance("LB-AL-A4")
def test_lb_al_a4_a_decision_is_kind_decided_in_the_acts_words(client, gate, node_signer):
    from axiom.extensions.builtins.attest import service

    action = _held(gate)
    rec = sign_decision(present_decision(action, approving=False), node_signer).record
    row = journal_rows.from_brief(service.brief(rec))
    assert row.lane == "attestations"
    assert row.subject == "attest.approvals.decided"
    assert row.summary == f"Rejected: {TITLE}"
    assert "ACTION_DECISION" not in row.summary
    assert (row.ref["decision"], row.ref["action_id"]) == ("rejected", action.action_id)
    assert row.link == f"/api/v1/attest/decisions/{action.action_id}"
    assert action.action_id in row.subjects
    as_json = {"subject": row.subject, "actor": row.actor, "status": row.status, "ref": row.ref}
    assert RowFilter(kind="decided").matches(as_json)
    assert not RowFilter(kind="decided").matches({**as_json, "subject": "attest.demo_log.signed"})
    with pytest.raises(ValueError, match="unknown kind"):
        RowFilter.of(mine=False, agent=None, levels=None, caller=None, kind="Decided; drop")


@pytest.mark.acceptance("LB-AL-A4")
def test_lb_al_a4_the_kind_filter_selects_decided_rows_in_the_query_and_forward(live):
    import uuid
    from datetime import UTC, datetime

    from axiom.extensions.builtins.journal import service as journal
    from axiom.extensions.builtins.journal import writer
    from axiom.extensions.builtins.journal.rows import Row

    from .test_j1 import caller

    def put(subject: str, summary: str) -> dict:
        return writer.append(Row(lane="platform", subject=subject, source="test", source_id=uuid.uuid4().hex,
                                 at=datetime.now(UTC), status="ok", summary=summary, actor="@op1:site-a",
                                 site="site-a"))  # fmt: skip

    put("test.thing.signed", "an entry")
    first = put("test.thing.decided", "Approved: one")
    put("test.decided_not.signed", "not a decision")
    put("test.thing.decided", "Rejected: two")
    me = caller("@op1:site-a")
    got = [r["summary"] for r in journal.query(me, kind="decided")["rows"]]
    assert got == ["Rejected: two", "Approved: one"]
    forward = [r["summary"] for r in journal.after(me, after=first["seq"] - 1, kind="decided")["rows"]]
    assert forward == ["Approved: one", "Rejected: two"]


@pytest.mark.acceptance("LB-AL-A5")
def test_lb_al_a5_a_timeline_row_names_its_actor_as_team_does(live, monkeypatch):
    """Activity shows the person's name, not their handle: each row carries ``actor_name`` from the
    people Team lists (``actors.names_by_handle``), null where nobody here answers to the handle."""
    import uuid
    from datetime import UTC, datetime

    from axiom.extensions.builtins.actors import directory
    from axiom.extensions.builtins.journal import service as journal, writer
    from axiom.extensions.builtins.journal.rows import Row

    from .test_j1 import caller

    rows = [{"handle": "@op1:site-a", "name": "Op One", "kind": "person"},
            {"handle": "@helper", "name": "Report helper", "kind": "agent"}]  # fmt: skip
    monkeypatch.setattr(directory, "build_rows", lambda agents=None: (rows, {}))
    monkeypatch.setitem(directory._NAMES_CACHE, "at", None)  # noqa: SLF001 - start with no names read
    monkeypatch.setitem(directory._NAMES_CACHE, "by", {})  # noqa: SLF001 - and leave none behind
    for actor in ("@op1:site-a", "@helper:site-a", "@nobody:site-a"):
        writer.append(Row(lane="platform", subject="test.thing.decided", source="test", source_id=uuid.uuid4().hex,
                          at=datetime.now(UTC), status="ok", summary=actor, actor=actor, site="site-a"))  # fmt: skip
    got = {r["actor"]: r["actor_name"] for r in journal.query(caller("@op1:site-a"))["rows"]}
    assert got == {"@op1:site-a": "Op One", "@helper:site-a": "Report helper", "@nobody:site-a": None}
    forward = journal.after(caller("@op1:site-a"), after=0)["rows"]
    assert {r["actor"]: r["actor_name"] for r in forward} == got


@pytest.mark.acceptance("LB-AL-A2")
def test_lb_al_a2_a_long_due_list_never_pushes_an_approval_out_of_needs_you(monkeypatch):
    from axiom.extensions.builtins.notifications import needs_you as ny

    due = [{"id": f"d{i}", "section": "due", "kind": "notice", "title": f"notice {i}"} for i in range(60)]
    ask = {"id": "attest:approve:a1", "section": "approve", "kind": "approve", "title": TITLE}
    monkeypatch.setattr(ny, "_feedback_items", lambda person: [])
    monkeypatch.setattr(ny, "_inbox_items", lambda person, store, limit: due)
    monkeypatch.setattr(ny, "_due_items", lambda person, provider: [])
    monkeypatch.setattr(ny, "_held_items", lambda person, path: [])
    monkeypatch.setattr(ny, "_attest_items", lambda person: [ask])
    got = ny.needs_you("@op2:site-a", inbox_store=object(), limit=50)
    sections = {s["id"]: len(s["items"]) for s in got["sections"]}
    assert sections["due"] == 50 and sections["approve"] == 1
    assert got["count"] == len(got["items"]) == 51
