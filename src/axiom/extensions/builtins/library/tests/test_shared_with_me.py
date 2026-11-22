"""Shared with me (ADR-263, as Google Drive): what a person was given by name is listed there.

A file shared with you is in your Shared with me, not inside the owner's folder in your tree. A
folder shared with you is listed there too, and opening it shows what it holds.
"""

from __future__ import annotations

from axiom.extensions.builtins.library.tests.test_access_on_every_row import ALICE, API, BOB, CY, _item


def _shared(client, headers):
    r = client.get(f"{API}/shared", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def test_a_file_shared_by_name_is_in_the_persons_shared_with_me(client):
    item = _item(client, "given.md", visibility="private")
    r = client.post(f"{API}/items/{item['id']}/share", json={"with": "@cy:alpha", "role": "viewer", "notify": False},
                    headers=ALICE)
    assert r.status_code == 200, r.text
    rows = {i["id"]: i for i in _shared(client, CY)["items"]}
    assert rows[item["id"]]["shared_via"] == "shared" and rows[item["id"]]["shared_by"] == "@alice:alpha"
    assert rows[item["id"]]["access"]["words"] == "You can view"
    assert item["id"] not in {i["id"] for i in _shared(client, BOB)["items"]}
    assert item["id"] not in {i["id"] for i in _shared(client, ALICE)["items"]}  # your own is never shared with you


def test_a_folder_shared_by_name_is_listed_and_opens_to_what_it_holds(client):
    path = client.post(f"{API}/book/folders", json={"label": "Team notes"}, headers=ALICE).json()["path"]
    inside = _item(client, "inside.md", folder=path, visibility="private")
    assert _shared(client, CY)["folders"] == []
    r = client.post(f"{API}/book/folders/{path}/share", json={"with": "@cy:alpha", "role": "viewer", "notify": False},
                    headers=ALICE)
    assert r.status_code == 200, r.text
    folders = _shared(client, CY)["folders"]
    assert [(f["path"], f["label"], f["owner"], f["shared_by"], f["role"]) for f in folders] == [
        (path, "Team notes", "@alice:alpha", "@alice:alpha", "viewer")]
    assert folders[0]["shared_at"]
    assert _shared(client, BOB)["folders"] == []
    opened = client.get(f"{API}/items", params={"folder": path}, headers=CY).json()["items"]
    assert [i["id"] for i in opened] == [inside["id"]]
    # the file inside reached Cy through the folder: it is not listed again as a file of its own
    assert inside["id"] not in {i["id"] for i in _shared(client, CY)["items"]}
