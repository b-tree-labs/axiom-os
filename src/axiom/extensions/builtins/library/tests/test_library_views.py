# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The browser's common views: Starred, Recent, Shared with me, As uploaded.

Each is per person and per site. Two people at one site and one person at
another play every case, the same three the upload tests use.
"""

from __future__ import annotations

import hashlib

import pytest

from axiom.extensions.builtins.library.tests.test_library_upload import (
    ALICE,
    ALICE_COLLEAGUE,
    DOC,
    MALLORY,
    _up,
)

BASE = "/api/v1/library"
OTHER = b"# Valve log\n\nThe spring valve was cycled twice and held.\n"


def _ids(answer: dict) -> list[str]:
    return [i["id"] for i in answer["items"]]


# --- Starred ----------------------------------------------------------------------


@pytest.mark.acceptance("MVP-04-A1")
def test_a_star_is_the_starrers_own_and_is_idempotent(client):
    item = _up(client, "pump.md", DOC).json()["items"][0]
    for _ in range(2):
        assert client.put(f"{BASE}/items/{item['id']}/star", headers=ALICE).json() == {
            "id": item["id"],
            "starred": True,
        }
    assert _ids(client.get(f"{BASE}/starred", headers=ALICE).json()) == [item["id"]]
    # a colleague who can read the document has not starred it
    assert client.get(f"{BASE}/starred", headers=ALICE_COLLEAGUE).json()["items"] == []
    for _ in range(2):
        assert client.delete(f"{BASE}/items/{item['id']}/star", headers=ALICE).status_code == 200
    assert client.get(f"{BASE}/starred", headers=ALICE).json()["items"] == []


def test_the_newest_star_comes_first(client):
    a = _up(client, "pump.md", DOC).json()["items"][0]
    b = _up(client, "valve.md", OTHER).json()["items"][0]
    client.put(f"{BASE}/items/{a['id']}/star", headers=ALICE)
    client.put(f"{BASE}/items/{b['id']}/star", headers=ALICE)
    assert _ids(client.get(f"{BASE}/starred", headers=ALICE).json()) == [b["id"], a["id"]]


def test_another_site_cannot_star_or_see_a_star(client):
    item = _up(client, "pump.md", DOC).json()["items"][0]
    assert client.put(f"{BASE}/items/{item['id']}/star", headers=MALLORY).status_code == 404
    client.put(f"{BASE}/items/{item['id']}/star", headers=ALICE)
    assert client.get(f"{BASE}/starred", headers=MALLORY).json() == {"items": [], "folders": []}


def test_a_star_on_something_no_longer_readable_is_not_served(client):
    item = _up(client, "pump.md", DOC).json()["items"][0]
    assert client.put(f"{BASE}/items/{item['id']}/star", headers=ALICE_COLLEAGUE).status_code == 200
    assert _ids(client.get(f"{BASE}/starred", headers=ALICE_COLLEAGUE).json()) == [item["id"]]
    # the owner takes it private: Bob's star stays, but the document is no longer his to see
    client.patch(f"{BASE}/items/{item['id']}", json={"visibility": "private"}, headers=ALICE)
    assert client.get(f"{BASE}/starred", headers=ALICE_COLLEAGUE).json()["items"] == []


def test_a_deleted_document_drops_out_of_starred_until_restored(client):
    item = _up(client, "pump.md", DOC).json()["items"][0]
    client.put(f"{BASE}/items/{item['id']}/star", headers=ALICE)
    client.delete(f"{BASE}/items/{item['id']}", headers=ALICE)
    assert client.get(f"{BASE}/starred", headers=ALICE).json()["items"] == []
    client.post(f"{BASE}/items/{item['id']}/restore", headers=ALICE)
    assert _ids(client.get(f"{BASE}/starred", headers=ALICE).json()) == [item["id"]]


def test_a_data_book_folder_can_be_starred_by_path(client):
    folders = client.get(f"{BASE}/book", headers=ALICE).json()["folders"]
    path = folders[0]["path"]
    assert client.put(f"{BASE}/stars/folders/{path}", headers=ALICE).json() == {
        "path": path,
        "starred": True,
    }
    assert [f["path"] for f in client.get(f"{BASE}/starred", headers=ALICE).json()["folders"]] == [
        path
    ]
    assert client.get(f"{BASE}/starred", headers=ALICE_COLLEAGUE).json()["folders"] == []
    assert client.put(f"{BASE}/stars/folders/no/such/folder", headers=ALICE).status_code == 404
    client.delete(f"{BASE}/stars/folders/{path}", headers=ALICE)
    assert client.get(f"{BASE}/starred", headers=ALICE).json()["folders"] == []


# --- Recent ------------------------------------------------------------------------


@pytest.mark.acceptance("MVP-04-A2")
def test_recent_is_what_you_uploaded_and_what_you_opened(client):
    mine = _up(client, "pump.md", DOC).json()["items"][0]
    theirs = _up(client, "valve.md", OTHER, headers=ALICE_COLLEAGUE).json()["items"][0]
    recent = client.get(f"{BASE}/recent", headers=ALICE).json()["items"]
    assert [(r["id"], r["recent_reason"]) for r in recent] == [(mine["id"], "uploaded")]

    assert client.post(f"{BASE}/items/{theirs['id']}/opened", headers=ALICE).status_code == 200
    recent = client.get(f"{BASE}/recent", headers=ALICE).json()["items"]
    assert [(r["id"], r["recent_reason"]) for r in recent] == [
        (theirs["id"], "opened"),
        (mine["id"], "uploaded"),
    ]

    # opening your own upload moves it to the top, as opened
    client.post(f"{BASE}/items/{mine['id']}/opened", headers=ALICE)
    assert [
        r["recent_reason"] for r in client.get(f"{BASE}/recent", headers=ALICE).json()["items"]
    ] == ["opened", "opened"]


def test_a_colleagues_opens_are_not_your_recent(client):
    item = _up(client, "pump.md", DOC).json()["items"][0]
    client.post(f"{BASE}/items/{item['id']}/opened", headers=ALICE_COLLEAGUE)
    assert [
        r["id"] for r in client.get(f"{BASE}/recent", headers=ALICE_COLLEAGUE).json()["items"]
    ] == [item["id"]]
    # Alice's own Recent has it because she uploaded it, not because Bob opened it
    assert [
        r["recent_reason"] for r in client.get(f"{BASE}/recent", headers=ALICE).json()["items"]
    ] == ["uploaded"]


def test_recent_never_crosses_sites_and_cannot_record_an_unreadable_open(client):
    item = _up(client, "pump.md", DOC).json()["items"][0]
    assert client.post(f"{BASE}/items/{item['id']}/opened", headers=MALLORY).status_code == 404
    assert client.get(f"{BASE}/recent", headers=MALLORY).json()["items"] == []


def test_a_deleted_document_leaves_recent(client):
    item = _up(client, "pump.md", DOC).json()["items"][0]
    client.delete(f"{BASE}/items/{item['id']}", headers=ALICE)
    assert client.get(f"{BASE}/recent", headers=ALICE).json()["items"] == []


# --- Shared with me ----------------------------------------------------------------


@pytest.mark.acceptance("MVP-04-A1")
def test_shared_with_me_is_what_colleagues_filed_for_the_site(client):
    item = _up(client, "pump.md", DOC).json()["items"][0]
    _up(client, "private.md", OTHER, visibility="private")
    answer = client.get(f"{BASE}/shared", headers=ALICE_COLLEAGUE).json()
    assert _ids(answer) == [item["id"]]
    assert answer["items"][0]["shared_by"] == "@alice:alpha"
    assert answer["basis"] == "site" and answer["grants"] is True and "SHR-R9.2" in answer["note"]
    assert answer["items"][0]["shared_via"] == "site"
    # your own documents are not shared *with* you
    assert client.get(f"{BASE}/shared", headers=ALICE).json()["items"] == []


def test_shared_with_me_lists_a_share_by_name_first(client, library):
    from axiom.extensions.builtins.library import access
    from axiom.extensions.builtins.library.db_models import LibraryItem
    from axiom.governance.access import decision, sharing

    site_item = _up(client, "pump.md", DOC).json()["items"][0]
    private = _up(client, "private.md", OTHER, visibility="private").json()["items"][0]
    with library["factory"]() as s:
        obj = access.item_object(s.get(LibraryItem, private["id"]), {})
    sharing.share(decision.who("@alice:alpha", "alpha"), obj, "@bob:alpha", "viewer")
    answer = client.get(f"{BASE}/shared", headers=ALICE_COLLEAGUE).json()
    assert _ids(answer) == [private["id"], site_item["id"]]
    first = answer["items"][0]
    assert first["shared_via"] == "shared" and first["shared_by"] == "@alice:alpha" and first["role"] == "viewer"


def test_shared_with_me_never_crosses_sites(client):
    _up(client, "pump.md", DOC)
    assert client.get(f"{BASE}/shared", headers=MALLORY).json()["items"] == []


# --- As uploaded -------------------------------------------------------------------


@pytest.mark.acceptance("MVP-04-A1")
def test_as_uploaded_lists_each_file_as_it_came_in(client):
    item = _up(client, "Pump Notes.md", DOC, title="Quarterly pump maintenance").json()["items"][0]
    answer = client.get(f"{BASE}/originals", headers=ALICE).json()
    assert answer["total"] == 1 and answer["page"] == 1
    row = answer["items"][0]
    # the name as given, never the generated title
    assert row["file_name"] == "Pump Notes.md" and row["item_id"] == item["id"]
    assert row["sha256"] == hashlib.sha256(DOC).hexdigest() and row["size"] == len(DOC)
    assert row["uploaded_by"] == "@alice:alpha" and row["source"] == "http"
    assert row["download_href"] == f"{BASE}/items/{item['id']}/content?download=true"
    # its labels travel with it (MVP-04-A1)
    assert row["label"] == item["label"] and row["ec"] == item["ec"]
    got = client.get(row["download_href"], headers=ALICE)
    assert got.status_code == 200 and got.content == DOC
    assert got.headers["content-disposition"].startswith("attachment")


def test_as_uploaded_narrows_to_one_upload_and_pages(client):
    first = _up(client, "pump.md", DOC).json()["items"][0]
    _up(client, "valve.md", OTHER)
    narrowed = client.get(
        f"{BASE}/originals", params={"job": first["job_id"]}, headers=ALICE
    ).json()
    assert [r["item_id"] for r in narrowed["items"]] == [first["id"]]
    page = client.get(f"{BASE}/originals", params={"page_size": 1, "page": 2}, headers=ALICE).json()
    assert page["total"] == 2 and [r["item_id"] for r in page["items"]] == [first["id"]]


@pytest.mark.acceptance("MVP-04-A1")
def test_as_uploaded_follows_the_same_scope_as_everything_else(client):
    _up(client, "pump.md", DOC)
    _up(client, "private.md", OTHER, visibility="private")
    assert client.get(f"{BASE}/originals", headers=MALLORY).json() == {
        "items": [],
        "total": 0,
        "page": 1,
        "page_size": 50,
    }
    colleague = client.get(f"{BASE}/originals", headers=ALICE_COLLEAGUE).json()
    assert [r["file_name"] for r in colleague["items"]] == ["pump.md"] and colleague["total"] == 1
    assert client.get(f"{BASE}/originals", headers=ALICE).json()["total"] == 2


def test_every_view_needs_a_principal(client):
    for path in ("/starred", "/recent", "/shared", "/originals"):
        assert client.get(BASE + path).status_code == 401
