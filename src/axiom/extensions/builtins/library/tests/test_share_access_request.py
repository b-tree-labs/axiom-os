# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Share, access, transfer and request access, on the skills every surface uses (SHR-R3.5, R5, R6).

The CLI, MCP and chat call the same ``library.*`` skills (DC-16); these tests
call them through the registry, and once through the CLI.
"""

from __future__ import annotations

import logging

import pytest

from axiom.extensions.builtins.library import sharing as lib_sharing
from axiom.extensions.builtins.library.db_models import LibraryEvent, LibraryItem
from axiom.extensions.builtins.library.tests.test_library_upload import _up

API = "/api/v1/library"
ALICE = {"x-test-principal": "@alice:alpha", "x-test-site": "alpha"}
BOB = {"x-test-principal": "@bob:alpha", "x-test-site": "alpha"}
CY = {"x-test-principal": "@cy:alpha", "x-test-site": "alpha"}
PROSE = "\n\nNotes kept for the team, about the quarterly review and what it found.\n"


@pytest.fixture
def told(library):
    sent: list[tuple[str, str, str]] = []
    lib_sharing.set_notifier(lambda to, summary, body, dedup: sent.append((to, summary, body)))
    yield sent
    lib_sharing.set_notifier(None)


@pytest.fixture
def skills(library, tmp_path):
    from axiom.extensions.builtins.library.skills import bind_default
    from axiom.infra.principal import PrincipalContext
    from axiom.infra.skills import SkillContext

    reg = bind_default()

    def as_(handle: str):
        return SkillContext(registry=reg, state_dir=tmp_path, logger=logging.getLogger("t"),
                            principal=PrincipalContext(handle=handle), surface="mcp")

    def call(handle: str, name: str, **params):
        return reg.invoke(name, params, as_(handle))

    return call


def _item(client, name="notes.md", headers=ALICE, **fields):
    r = _up(client, name, ("# " + name + PROSE).encode(), headers=headers, **fields)
    assert r.status_code == 201, r.text
    return r.json()["items"][0]


def test_shr_r3_5_share_access_and_remove_on_the_skills(client, library, skills, told):
    item = _item(client, visibility="private")
    shared = skills("@alice:alpha", "library.share", id=item["id"], **{"with": "@cy:alpha"}, role="editor",
                    message="for the review")
    assert shared.ok, shared.errors
    assert shared.value["words"] == "@alice:alpha gave @cy:alpha editor access" and shared.value["receipt_id"]
    assert told == [("@cy:alpha", "@alice:alpha shared “notes.md” with you (editor)", "for the review")]
    assert client.get(f"{API}/items/{item['id']}", headers=CY).status_code == 200

    seen = skills("@cy:alpha", "library.access", id=item["id"])
    assert seen.ok and seen.value["owner"] == "@alice:alpha" and seen.value["your_role"] == "editor"
    assert [p["subject"] for p in seen.value["people"]] == ["@cy:alpha"]

    removed = skills("@alice:alpha", "library.share", id=item["id"], **{"with": "@cy:alpha"}, remove=True)
    assert removed.ok, removed.errors
    assert client.get(f"{API}/items/{item['id']}", headers=CY).status_code == 404
    with library["factory"]() as s:
        words = [e.detail["words"] for e in s.query(LibraryEvent).filter_by(item_id=item["id"])
                 if e.action in ("share", "unshare")]
    assert words == ["@alice:alpha gave @cy:alpha editor access", "@alice:alpha removed @cy:alpha's access"]


def test_general_access_moves_visibility_and_retrieval(client, library, skills):
    item = _item(client, visibility="private")
    assert client.get(f"{API}/items/{item['id']}", headers=BOB).status_code == 404
    out = skills("@alice:alpha", "library.share", id=item["id"], general="site:viewer")
    assert out.ok, out.errors
    with library["factory"]() as s:
        row = s.get(LibraryItem, item["id"])
        assert row.visibility == "site" and row.corpus == "rag-org"
    assert client.get(f"{API}/items/{item['id']}", headers=BOB).status_code == 200
    assert skills("@alice:alpha", "library.share", id=item["id"], general="restricted").ok
    with library["factory"]() as s:
        assert s.get(LibraryItem, item["id"]).visibility == "private"
    assert client.get(f"{API}/items/{item['id']}", headers=BOB).status_code == 404


def test_commenter_waits_for_comments(client, skills):
    item = _item(client)
    out = skills("@alice:alpha", "library.share", id=item["id"], **{"with": "@cy:alpha"}, role="commenter")
    assert not out.ok and "once comments exist" in out.errors[0]


def test_shr_a5_settings_through_the_skill(client, skills):
    item = _item(client, visibility="private")
    assert skills("@alice:alpha", "library.share", id=item["id"], **{"with": "@cy:alpha"}, role="editor").ok
    assert skills("@alice:alpha", "library.share", id=item["id"], editors_can_share=False).ok
    refused = skills("@cy:alpha", "library.share", id=item["id"], **{"with": "@bob:alpha"}, role="viewer")
    assert not refused.ok and "turned off sharing by editors" in refused.errors[0]
    why = skills("@cy:alpha", "library.access", id=item["id"]).value
    assert why["may_share"] is False and "turned off" in why["why_not"]


def test_shr_a8_a_share_to_someone_not_cleared_is_refused_naming_the_label(client, library, skills, monkeypatch):
    from axiom.governance import classification as gc

    item = _item(client, visibility="private")
    with library["factory"]() as s:
        row = s.get(LibraryItem, item["id"])
        row.ec_label = gc.confirmed_label("controlled", by="@alice:alpha").to_json()
        s.commit()
    monkeypatch.setattr(gc, "clearance_for", lambda p: gc.Classification.from_str("internal")
                        if p == "@cy:alpha" else gc.Classification.from_str("controlled"))
    out = skills("@alice:alpha", "library.share", id=item["id"], **{"with": "@cy:alpha"}, role="viewer")
    assert not out.ok and "controlled" in out.errors[0] and "notes" not in out.errors[0]


def test_shr_r5_transfer_makes_the_old_owner_an_editor(client, library, skills, told):
    item = _item(client, visibility="private")
    refused = skills("@alice:alpha", "library.transfer", id=item["id"], to="@cy:alpha")
    assert not refused.ok and "already on the access list" in refused.errors[0]
    skills("@alice:alpha", "library.share", id=item["id"], **{"with": "@cy:alpha"}, role="viewer", notify=False)
    moved = skills("@alice:alpha", "library.transfer", id=item["id"], to="@cy:alpha")
    assert moved.ok, moved.errors
    with library["factory"]() as s:
        row = s.get(LibraryItem, item["id"])
        assert row.owner == "@cy:alpha" and row.corpus.endswith("cy-alpha")
    assert skills("@cy:alpha", "library.access", id=item["id"]).value["your_role"] == "owner"
    assert skills("@alice:alpha", "library.access", id=item["id"]).value["your_role"] == "editor"
    assert told[-1][0] == "@cy:alpha"


def test_shr_a7_request_reaches_the_owner_share_lower_decline_and_request_again(client, skills, told):
    item = _item(client, visibility="private")
    # The address alone reveals nothing: no title, owner or folder in the answer.
    asked = skills("@cy:alpha", "library.request", id=item["id"], role="editor", note="for the audit")
    assert asked.ok, asked.errors
    assert asked.value["state"] == "open" and asked.value["sent_to"] == "the owner"
    assert "notes" not in str(asked.value) and "@alice" not in str(asked.value)
    assert told[-1][0] == "@alice:alpha" and "asks for editor access" in told[-1][1]  # the Owner's Needs you

    open_ = skills("@alice:alpha", "library.request", id=item["id"], action="list")
    assert [r["requester"] for r in open_.value] == ["@cy:alpha"]
    approved = skills("@alice:alpha", "library.request", id=item["id"], action="approve",
                      request_id=asked.value["id"], role="viewer")
    assert approved.ok and approved.value["granted_role"] == "viewer"
    assert skills("@cy:alpha", "library.access", id=item["id"]).value["your_role"] == "viewer"

    again = skills("@cy:alpha", "library.request", id=item["id"], role="editor")
    declined = skills("@alice:alpha", "library.request", id=item["id"], action="decline",
                      request_id=again.value["id"], reason="Not until the audit closes")
    assert declined.ok, declined.errors
    assert told[-1] == ("@cy:alpha", "@alice:alpha declined your request", "Not until the audit closes")
    status = skills("@cy:alpha", "library.request", id=item["id"], action="status").value
    assert status["previously_refused"]["by"] == "@alice:alpha"
    assert status["previously_refused"]["reason"] == "Not until the audit closes"
    assert skills("@cy:alpha", "library.request", id=item["id"], role="editor").value["state"] == "open"


def test_a_viewer_cannot_answer_and_a_stranger_cannot_list_requests(client, skills):
    item = _item(client)
    asked = skills("@cy:alpha", "library.request", id=item["id"], role="editor")
    out = skills("@bob:alpha", "library.request", id=item["id"], action="approve", request_id=asked.value["id"])
    assert not out.ok
    assert not skills("@bob:alpha", "library.request", id=item["id"], action="list").ok


def test_withdraw_and_one_reminder_a_day(client, skills, told):
    item = _item(client, visibility="private")
    asked = skills("@cy:alpha", "library.request", id=item["id"])
    assert not skills("@cy:alpha", "library.request", id=item["id"], action="remind",
                      request_id=asked.value["id"]).ok  # asked less than 24 hours ago
    assert skills("@cy:alpha", "library.request", id=item["id"], action="withdraw",
                  request_id=asked.value["id"]).value["state"] == "withdrawn"


def test_a_folder_is_shared_the_same_way(client, skills):
    path = client.post(f"{API}/book/folders", json={"label": "Team"}, headers=ALICE).json()["path"]
    inside = _item(client, folder=path, visibility="private")
    assert client.get(f"{API}/items/{inside['id']}", headers=CY).status_code == 404
    assert skills("@alice:alpha", "library.share", id=f"folder:{path}", **{"with": "@cy:alpha"}, role="viewer").ok
    assert client.get(f"{API}/items/{inside['id']}", headers=CY).status_code == 200
    assert path in {f["path"] for f in client.get(f"{API}/book", headers=CY).json()["folders"]}


def test_the_cli_reaches_the_same_skills(client, library, capsys, monkeypatch):
    from axiom.extensions.builtins.library import cli

    item = _item(client, visibility="private")
    monkeypatch.setenv("AXIOM_PRINCIPAL", "@alice:alpha")
    monkeypatch.setattr("axiom.extensions.builtins.library.skills.verbs._caller",
                        lambda ctx, params: __import__("axiom.extensions.builtins.library.scope",
                                                       fromlist=["make_caller"]).make_caller("@alice:alpha", "alpha"))
    assert cli.main(["share", item["id"], "--with", "@cy:alpha", "--role", "viewer", "--no-notify"]) == 0
    assert "gave @cy:alpha viewer access" in capsys.readouterr().out
    assert cli.main(["access", item["id"]]) == 0
    out = capsys.readouterr().out
    assert "@alice:alpha" in out and "@cy:alpha" in out


def test_the_http_face_offers_the_same_acts(client, told):
    item = _item(client, visibility="private")
    r = client.post(f"{API}/items/{item['id']}/share", json={"with": "@cy:alpha", "role": "viewer"}, headers=ALICE)
    assert r.status_code == 200, r.text
    assert client.get(f"{API}/items/{item['id']}/access", headers=CY).json()["your_role"] == "viewer"
    asked = client.post(f"{API}/items/{item['id']}/requests", json={"role": "editor"}, headers=BOB)
    assert asked.status_code == 200 and asked.json()["state"] == "open"
    assert client.post(f"{API}/items/{item['id']}/share", json={"with": "@bob:alpha"}, headers=BOB).status_code == 404
