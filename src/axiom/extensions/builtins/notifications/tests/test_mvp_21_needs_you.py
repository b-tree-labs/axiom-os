# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

""""Needs you": one person's queue, and its count is its list (MVP-21).

- the queue shows only the caller's own items, from every source (inbox, the
  agenda's due soon, held drafts), and the count equals the list (MVP-21-A1);
- the person is the caller, never a parameter: naming somebody else does not
  read their queue, on the skill or the web route;
- the most frequent due task is named as ``top`` so the attention bar can put
  it one click from landing (MVP-21-A2, the server half);
- reading the queue writes nothing.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from axiom.extensions.builtins.notifications import inbox_db
from axiom.extensions.builtins.notifications.inbox import InMemoryInboxStore
from axiom.extensions.builtins.notifications.needs_you import needs_you
from axiom.governance import Classification
from axiom.infra import audit_trail
from axiom.infra.orchestrator.actions import Action
from axiom.infra.orchestrator.approval_store import FileActionStore
from axiom.infra.skills import SkillContext, SkillRegistry

ME, OTHER = "@jsmith:example", "@other:example"


def _inbox() -> InMemoryInboxStore:
    store = InMemoryInboxStore()
    for who, summary in ((ME, "approve the invitation for @new"), (OTHER, "their alert"),
                         (ME, "intake request: sample 7")):
        store.write(recipient=who, receipt_id=f"r-{summary}", classification=Classification.INTERNAL,
                    priority="normal", summary=summary)
    return store


def _due(person: str, days: int):
    by_person = {
        ME: [
            {"id": "vault:k1", "kind": "expiry", "title": "key 'laptop CLI' expires",
             "source": "vault", "start": "2026-11-20", "date_only": True, "owner_link": "/account#keys"},
            {"id": "vault:k2", "kind": "expiry", "title": "key 'notebook' expires",
             "source": "vault", "start": "2026-11-05", "date_only": True, "owner_link": "/account#keys"},
            {"id": "attest:q1", "kind": "obligation", "title": "requalification due",
             "source": "attest", "start": "2026-11-02", "date_only": True, "owner_link": "/log"},
        ],
        OTHER: [{"id": "vault:x", "kind": "expiry", "title": "their key", "source": "vault",
                 "start": "2026-11-03", "date_only": True}],
    }
    return by_person.get(person, [])


def _approvals(tmp_path: Path) -> Path:
    path = tmp_path / "state" / "orchestrator" / "approvals.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    store = FileActionStore(path)
    store.put(Action(name="settings.set", params={"key": "a"}, requested_by=ME, reason="site rule"))
    store.put(Action(name="flags.set", params={"key": "b"}, requested_by=OTHER, reason="site rule"))
    return path


@pytest.mark.acceptance("MVP-21-A1")
def test_the_queue_shows_only_my_items_and_the_count_matches_the_list(tmp_path):
    got = needs_you(ME, inbox_store=_inbox(), approvals_path=_approvals(tmp_path), due_provider=_due)
    titles = [i["title"] for i in got["items"]]
    assert "their alert" not in titles and "their key" not in titles
    assert not any("flags.set" in t for t in titles)
    assert got["count"] == len(got["items"]) == 6
    assert sum(len(s["items"]) for s in got["sections"]) == got["count"]
    # Logbook unit X1 adds To sign and Co-sign after these two.
    assert [s["label"] for s in got["sections"]] == ["Due", "Held drafts", "To sign", "Co-sign"]
    assert [i["title"] for i in got["sections"][1]["items"]] == ["settings.set is held for approval"]
    assert got["name"] == "Needs you"


@pytest.mark.acceptance("MVP-21-A1")
def test_the_person_is_the_caller_never_a_parameter(tmp_path):
    from axiom.extensions.builtins.notifications import skills as note_skills

    inbox_db.set_default_inbox_store(_inbox())
    try:
        reg = SkillRegistry()
        note_skills.bind(reg)
        ctx = SkillContext(registry=reg, state_dir=tmp_path / "state", logger=logging.getLogger("t"),
                           actor=ME)
        got = reg.invoke("notifications.needs_you", {"recipient": OTHER, "person": OTHER}, ctx)
        assert got.ok and got.value["person"] == ME
        assert "their alert" not in [i["title"] for i in got.value["items"]]
        assert got.value["count"] == len(got.value["items"])
        # A read: nothing written, not even an audit row.
        assert not reg.is_mutating("notifications.needs_you")
        assert audit_trail.read_chain(audit_trail.audit_path(tmp_path / "state")) == []
    finally:
        inbox_db.set_default_inbox_store(None)


@pytest.mark.acceptance("MVP-21-A1")
def test_the_web_route_answers_for_the_signed_in_person_only(tmp_path, monkeypatch):
    from fastapi import APIRouter, FastAPI
    from fastapi.testclient import TestClient

    import axiom.webauth as webauth
    from axiom.extensions.builtins.notifications.needs_you_api import register_routes

    monkeypatch.setenv("AXI_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("AXIOM_AUTHORITY_RECEIPTS", "off")
    inbox_db.set_default_inbox_store(_inbox())
    try:
        router = APIRouter()
        register_routes(router)
        app = FastAPI()
        app.include_router(router, prefix="/api/v1")
        client = TestClient(app)
        monkeypatch.setattr(webauth, "session_from_cookies", lambda c: None)
        assert client.get("/api/v1/needs-you").status_code == 401
        monkeypatch.setattr(webauth, "session_from_cookies",
                            lambda c: {"sub": "jsmith", "email": "jsmith@example.org", "site": "example"})
        body = client.get("/api/v1/needs-you?recipient=@other:example").json()
        assert body["person"] == ME
        assert body["count"] == len(body["items"])
        assert "their alert" not in [i["title"] for i in body["items"]]
    finally:
        inbox_db.set_default_inbox_store(None)


@pytest.mark.acceptance("MVP-21-A2")
def test_the_most_frequent_due_task_is_named_soonest_first(tmp_path):
    got = needs_you(ME, inbox_store=InMemoryInboxStore(), approvals_path=None, due_provider=_due)
    # Two expiries and one obligation: the expiry is the most frequent kind, soonest first.
    assert got["top"]["kind"] == "expiry" and got["top"]["title"] == "key 'notebook' expires"
    assert got["top"]["href"] == "/account#keys"
    assert needs_you(ME, inbox_store=InMemoryInboxStore(), approvals_path=None,
                     due_provider=lambda p, d: [])["top"] is None


def test_a_failing_source_never_empties_the_queue(tmp_path):
    def broken(person, days):
        raise RuntimeError("agenda down")

    got = needs_you(ME, inbox_store=_inbox(), approvals_path=None, due_provider=broken)
    assert got["count"] == 2 and got["count"] == len(got["items"])
