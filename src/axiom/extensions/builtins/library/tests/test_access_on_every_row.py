# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""What the page needs to say, on every row, whose a file is and what you can do (SHR-R9.1, R9.2).

The web stage draws these; the server says them, so every surface reads the same words.
"""

from __future__ import annotations

from axiom.extensions.builtins.library.tests.test_library_upload import _up

API = "/api/v1/library"
ALICE = {"x-test-principal": "@alice:alpha", "x-test-site": "alpha"}
BOB = {"x-test-principal": "@bob:alpha", "x-test-site": "alpha"}
CY = {"x-test-principal": "@cy:alpha", "x-test-site": "alpha"}
PROSE = "\n\nNotes kept for the team, about the quarterly review and what it found.\n"


def _item(client, name, headers=ALICE, **fields):
    r = _up(client, name, ("# " + name + PROSE).encode(), headers=headers, **fields)
    assert r.status_code == 201, r.text
    return r.json()["items"][0]


def _rows(client, headers, **params):
    return {i["id"]: i for i in client.get(f"{API}/items", params=params, headers=headers).json()["items"]}


def test_shr_r9_1_each_row_says_whose_it_is_and_what_you_can_do(client):
    own_private = _item(client, "a.md", visibility="private")
    own_site = _item(client, "b.md")
    shared = _item(client, "c.md", visibility="private")
    assert client.post(f"{API}/items/{shared['id']}/share", json={"with": "@cy:alpha", "role": "editor",
                                                                  "notify": False}, headers=ALICE).status_code == 200
    mine = _rows(client, ALICE)
    assert mine[own_private["id"]]["access"] == {
        "mine": True, "role": "owner", "general": "restricted", "general_role": None, "people": 0, "via": "own",
        "words": "Only you"}
    assert mine[own_site["id"]]["access"]["words"] == "Everyone at alpha"
    assert mine[shared["id"]]["access"]["words"] == "You and 1 other"
    cy = _rows(client, CY)
    assert cy[shared["id"]]["access"]["words"] == "You can edit" and cy[shared["id"]]["access"]["via"] == "shared"
    bob = _rows(client, BOB)
    assert bob[own_site["id"]]["access"] == {
        "mine": False, "role": "viewer", "general": "site", "general_role": "viewer", "people": None, "via": "site",
        "words": "You can view"}  # someone else's: how many others is not theirs to know


def test_shr_r9_2_the_owner_filter(client):
    mine = _item(client, "mine.md")
    theirs = _item(client, "theirs.md", headers=BOB)
    assert set(_rows(client, ALICE, owner="me")) == {mine["id"]}
    assert set(_rows(client, ALICE, owner="others")) == {theirs["id"]}
    assert set(_rows(client, ALICE)) == {mine["id"], theirs["id"]}
    assert client.get(f"{API}/items", params={"owner": "nobody"}, headers=ALICE).status_code == 422


def test_one_item_carries_its_access_too(client):
    item = _item(client, "one.md")
    assert client.get(f"{API}/items/{item['id']}", headers=BOB).json()["access"]["words"] == "You can view"


def test_a_folder_is_shared_over_http(client):
    path = client.post(f"{API}/book/folders", json={"label": "Team"}, headers=ALICE).json()["path"]
    inside = _item(client, "in.md", folder=path, visibility="private")
    assert client.get(f"{API}/book/folders/{path}/access", headers=CY).status_code == 404
    r = client.post(f"{API}/book/folders/{path}/share", json={"with": "@cy:alpha", "role": "viewer", "notify": False},
                    headers=ALICE)
    assert r.status_code == 200, r.text
    seen = client.get(f"{API}/book/folders/{path}/access", headers=CY).json()
    assert seen["your_role"] == "viewer" and seen["owner"] == "@alice:alpha"
    assert client.get(f"{API}/items/{inside['id']}", headers=CY).status_code == 200
    asked = client.post(f"{API}/book/folders/{path}/requests", json={"role": "editor"}, headers=BOB)
    assert asked.status_code == 200 and asked.json()["state"] == "open"


def test_every_view_carries_the_same_access_words(client):
    item = _item(client, "seen.md")
    client.put(f"{API}/items/{item['id']}/star", headers=BOB)
    client.post(f"{API}/items/{item['id']}/opened", headers=BOB)
    for path in ("/starred", "/recent", "/shared"):
        body = client.get(f"{API}{path}", headers=BOB).json()
        rows = body if isinstance(body, list) else body.get("items", [])
        assert [r["access"]["words"] for r in rows if r["id"] == item["id"]] == ["You can view"], path
