# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Every Library verb asks the one decision point (prd-sharing-and-access, stage 1; #1952).

Two people at one site (Alice owns, Bob is a colleague), a third (Cy) given a
role, and a site-admin (Sam). Each test names its acceptance row.
"""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.library.db_models import LibraryEvent, LibraryFolder, LibraryItem
from axiom.extensions.builtins.library.tests.test_library_upload import (
    ALICE,
    ALICE_COLLEAGUE,
    _up,
)
from axiom.governance.access import decision, sharing

API = "/api/v1/library"
BOB = ALICE_COLLEAGUE
CY = {"x-test-principal": "@cy:alpha", "x-test-site": "alpha"}
SAM = {"x-test-principal": "@sam:alpha", "x-test-site": "alpha"}
BEA = {"x-test-principal": "@bea:alpha", "x-test-site": "alpha"}

PROSE = "\n\nNotes kept for the team, about the quarterly review and what it found.\n"


@pytest.fixture(autouse=True)
def site_roles(library):
    roles = {"@sam:alpha": ["site-admin"], "@bea:alpha": ["book-manager"]}
    decision.set_resolvers(groups=lambda p, s: [], roles=lambda p, s: roles.get(p, []))
    yield
    decision.set_resolvers()


def _item(client, name="notes.md", headers=ALICE, **fields):
    r = _up(client, name, ("# " + name + PROSE).encode(), headers=headers, **fields)
    assert r.status_code == 201, r.text
    return r.json()["items"][0]


def _share(library, item_id, who, role, by="@alice:alpha"):
    from axiom.extensions.builtins.library import access

    with library["factory"]() as s:
        row = s.get(LibraryItem, item_id)
        obj = access.item_object(row, access.user_folders(s, "alpha"))
    sharing.share(decision.who(by, "alpha"), obj, who, role)


def _share_folder(library, path, who, role, by="@alice:alpha"):
    from axiom.extensions.builtins.library import access

    with library["factory"]() as s:
        folders = access.user_folders(s, "alpha")
        obj = access.folder_object(folders[path], folders)
    sharing.share(decision.who(by, "alpha"), obj, who, role)


def _folder(client, label, headers=ALICE, parent=""):
    r = client.post(f"{API}/book/folders", json={"label": label, "parent": parent}, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()["path"]


# --- SHR-A1 ---------------------------------------------------------------------


def test_shr_a1_no_role_is_not_found_and_a_viewer_reads_but_cannot_change(client, library):
    item = _item(client, visibility="private")
    assert client.get(f"{API}/items/{item['id']}", headers=BOB).status_code == 404
    assert client.get(f"{API}/items/{item['id']}/content", headers=BOB).status_code == 404
    _share(library, item["id"], "@bob:alpha", "viewer")
    assert client.get(f"{API}/items/{item['id']}", headers=BOB).status_code == 200
    r = client.patch(f"{API}/items/{item['id']}", json={"title": "Mine now"}, headers=BOB)
    assert r.status_code == 403 and "Editor" in r.json()["detail"]
    assert client.delete(f"{API}/items/{item['id']}", headers=BOB).status_code == 403


def test_shr_a1_the_skill_surface_gives_the_same_answer(client, library, tmp_path):
    import logging

    from axiom.extensions.builtins.library.skills import bind_default
    from axiom.infra.principal import PrincipalContext
    from axiom.infra.skills import SkillContext

    item = _item(client, visibility="private")
    reg = bind_default()
    ctx = SkillContext(registry=reg, state_dir=tmp_path, logger=logging.getLogger("t"),
                       principal=PrincipalContext(handle="@bob:alpha"), surface="mcp")
    shown = reg.invoke("library.show", {"id": item["id"]}, ctx)
    assert not shown.ok and "need access" in shown.errors[0]
    _share(library, item["id"], "@bob:alpha", "viewer")
    assert reg.invoke("library.show", {"id": item["id"]}, ctx).ok
    moved = reg.invoke("library.move", {"id": item["id"], "folder": "documents"}, ctx)
    assert not moved.ok and "Editor" in moved.errors[0]


def test_an_editor_may_change_it_and_a_shared_item_is_listed_for_them(client, library):
    item = _item(client, visibility="private")
    assert item["id"] not in [i["id"] for i in client.get(f"{API}/items", headers=CY).json()["items"]]
    _share(library, item["id"], "@cy:alpha", "editor")
    assert item["id"] in [i["id"] for i in client.get(f"{API}/items", headers=CY).json()["items"]]
    r = client.patch(f"{API}/items/{item['id']}", json={"title": "Edited by Cy"}, headers=CY)
    assert r.status_code == 200 and r.json()["title"] == "Edited by Cy"


# --- SHR-A2 (#1952) --------------------------------------------------------------


def test_shr_a2_a_viewer_cannot_move_rename_or_delete_another_person_s_things(client, library):
    item = _item(client)  # Everyone at the site · Viewer
    path = _folder(client, "Calibration")
    r = client.post(f"{API}/items/{item['id']}/move", json={"folder": "documents"}, headers=BOB)
    assert r.status_code == 403 and "Editor" in r.json()["detail"]
    assert client.patch(f"{API}/book/folders/{path}", json={"label": "Bob's"}, headers=BOB).status_code in (403, 404)
    assert client.delete(f"{API}/book/folders/{path}", headers=BOB).status_code in (403, 404)
    with library["factory"]() as s:
        assert s.get(LibraryItem, item["id"]).folder == item["folder"]
        assert s.query(LibraryFolder).filter_by(path=path).one().label == "Calibration"


def test_shr_a2_an_editor_s_move_succeeds_and_is_recorded(client, library):
    item = _item(client)
    _share(library, item["id"], "@cy:alpha", "editor")
    r = client.post(f"{API}/items/{item['id']}/move", json={"folder": "documents"}, headers=CY)
    assert r.status_code == 200, r.text
    with library["factory"]() as s:
        moves = [e for e in s.query(LibraryEvent).filter_by(item_id=item["id"], action="move")]
        assert [e.principal for e in moves] == ["@cy:alpha"]


def test_shr_a2_moving_into_a_person_s_folder_needs_editor_on_the_folder(client, library):
    mine = _item(client, "bobs.md", headers=BOB)
    path = _folder(client, "Alice only")
    r = client.post(f"{API}/items/{mine['id']}/move", json={"folder": path}, headers=BOB)
    assert r.status_code in (403, 404)
    _share_folder(library, path, "@bob:alpha", "editor")
    assert client.post(f"{API}/items/{mine['id']}/move", json={"folder": path}, headers=BOB).status_code == 200


def test_shr_a2_only_a_book_manager_renames_a_scheme_category(client, library):
    r = client.patch(f"{API}/book/folders/code", json={"label": "Scripts"}, headers=ALICE)
    assert r.status_code == 403 and "Book manager" in r.json()["detail"]
    assert client.patch(f"{API}/book/folders/code", json={"label": "Scripts"}, headers=BEA).status_code == 200
    assert client.patch(f"{API}/book/folders/code", json={"label": "Code"}, headers=SAM).status_code == 200


# --- SHR-A3 ---------------------------------------------------------------------


def test_shr_a3_deleting_a_folder_takes_what_it_holds_in_one_undoable_act(client, library):
    top = _folder(client, "Project")
    sub = _folder(client, "Drafts", parent=top)
    a = _item(client, "a.md", folder=top)
    b = _item(client, "b.md", folder=sub)
    held = _item(client, "held.md", folder=sub)
    with library["factory"]() as s:
        s.get(LibraryItem, held["id"]).ec = {"verdict": "held", "reasons": ["review"]}
        s.commit()
    r = client.delete(f"{API}/book/folders/{top}", headers=ALICE)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["deleted_items"] == 2 and out["stayed"] == [{"count": 1, "why": "held for export-control review"}]
    assert out["undo"]
    with library["factory"]() as s:
        assert s.get(LibraryItem, a["id"]).deleted_at is not None
        assert s.get(LibraryItem, b["id"]).deleted_at is not None
        assert s.get(LibraryItem, held["id"]).deleted_at is None
        # The folders that keep a held item keep only those; nothing is orphaned.
        kept = {f.path for f in s.query(LibraryFolder).filter(LibraryFolder.deleted_at.is_(None))}
        assert {top, sub} <= kept
    restored = client.post(f"{API}/book/folders/restore", json={"act": out["undo"]}, headers=ALICE)
    assert restored.status_code == 200, restored.text
    with library["factory"]() as s:
        assert s.get(LibraryItem, a["id"]).deleted_at is None and s.get(LibraryItem, b["id"]).deleted_at is None


def test_shr_a3_an_empty_folder_and_its_subfolders_go_together(client, library):
    top = _folder(client, "Old")
    sub = _folder(client, "Older", parent=top)
    _item(client, "x.md", folder=sub)
    assert client.delete(f"{API}/book/folders/{top}", headers=ALICE).status_code == 200
    with library["factory"]() as s:
        live = {f.path for f in s.query(LibraryFolder).filter(LibraryFolder.deleted_at.is_(None))}
        assert top not in live and sub not in live


def test_an_editor_of_the_folder_deletes_it_but_only_what_they_may(client, library):
    top = _folder(client, "Shared work")
    _share_folder(library, top, "@cy:alpha", "editor")
    _item(client, "alice.md", folder=top)
    r = client.delete(f"{API}/book/folders/{top}", headers=CY)
    assert r.status_code == 200 and r.json()["deleted_items"] == 1


# --- SHR-A10 (content half) -----------------------------------------------------


def test_shr_a10_a_site_admin_cannot_open_a_restricted_item(client, library):
    item = _item(client, visibility="private")
    assert client.get(f"{API}/items/{item['id']}", headers=SAM).status_code == 404
    assert item["id"] not in [i["id"] for i in client.get(f"{API}/items", headers=SAM).json()["items"]]


# --- SHR-A11 --------------------------------------------------------------------


def _old_can_read(principal, row):
    return row.visibility == "site" or row.owner == principal


def test_shr_a11_every_person_reads_exactly_the_files_they_could_before(client, library):
    """Built before any share exists: the projection alone must reproduce today's reads."""
    from axiom.extensions.builtins.library import access
    from axiom.extensions.builtins.library.scope import make_caller

    a_folder = _folder(client, "Alice folder")
    b_folder = _folder(client, "Bob folder", headers=BOB)
    made = [
        _item(client, "a-site.md"), _item(client, "a-private.md", visibility="private"),
        _item(client, "a-in-folder.md", folder=a_folder), _item(client, "b-site.md", headers=BOB),
        _item(client, "b-private.md", headers=BOB, visibility="private"),
        _item(client, "b-in-alice-folder.md", headers=BOB, folder=a_folder),
        _item(client, "b-in-folder.md", headers=BOB, folder=b_folder, visibility="private"),
    ]
    people = ["@alice:alpha", "@bob:alpha", "@cy:alpha", "@sam:alpha"]
    with library["factory"]() as s:
        rows = [s.get(LibraryItem, m["id"]) for m in made]
        for p in people:
            caller = make_caller(p, "alpha")
            before = {r.id for r in rows if _old_can_read(p, r)}
            assert access.readable_ids(s, caller, rows) == before, p
    # ... and the listing says the same.
    for h in (ALICE, BOB, CY):
        listed = {i["id"] for i in client.get(f"{API}/items", headers=h).json()["items"]}
        with library["factory"]() as s:
            expect = {m["id"] for m in made if _old_can_read(h["x-test-principal"], s.get(LibraryItem, m["id"]))}
        assert listed == expect, h


def test_shr_a11_only_other_people_s_folder_names_leave_the_tree(client, library):
    a_folder = _folder(client, "Alice folder")
    tree = {f["path"] for f in client.get(f"{API}/book", headers=BOB).json()["folders"]}
    assert a_folder not in tree and "documents" in tree  # scheme categories stay for everyone
    assert a_folder in {f["path"] for f in client.get(f"{API}/book", headers=ALICE).json()["folders"]}


def test_the_classifier_never_files_into_a_folder_the_uploader_cannot_file_into(client, library):
    first = _item(client, "pump-calibration-2026.md")
    path = _folder(client, "Calibration")
    client.post(f"{API}/items/{first['id']}/move", json={"folder": path}, headers=ALICE)
    later = _item(client, "valve-calibration-2026.md", headers=BOB)
    assert later["folder"] != path  # filing there would have let the classifier decide who reads it


# --- the settings, through the Library -----------------------------------------


def test_shr_a5_download_off_refuses_a_viewer_s_download_and_copy(client, library):
    from axiom.extensions.builtins.library import access

    item = _item(client)
    with library["factory"]() as s:
        obj = access.item_object(s.get(LibraryItem, item["id"]), {})
    sharing.set_settings(decision.who("@alice:alpha", "alpha"), obj, viewers_can_download=False)
    assert client.get(f"{API}/items/{item['id']}/content", params={"download": True}, headers=BOB).status_code == 403
    assert client.post(f"{API}/items/{item['id']}/copy", headers=BOB).status_code == 403
    assert client.get(f"{API}/items/{item['id']}", headers=BOB).status_code == 200  # still opens


def test_restore_is_the_owner_s_or_the_deleter_s(client, library):
    top = _folder(client, "Team")
    _share_folder(library, top, "@cy:alpha", "editor")
    item = _item(client, "t.md", folder=top)
    assert client.delete(f"{API}/items/{item['id']}", headers=CY).status_code == 200
    assert client.post(f"{API}/items/{item['id']}/restore", headers=CY).status_code == 200



def test_a_group_member_not_cleared_for_the_label_gets_not_found(client, library):
    from axiom.governance import classification as gc

    item = _item(client, visibility="private")
    with library["factory"]() as s:
        s.get(LibraryItem, item["id"]).ec_label = gc.confirmed_label("controlled", by="@alice:alpha").to_json()
        s.commit()
    gc.set_clearance_resolver(lambda p: "controlled" if p in ("@alice:alpha", "@bob:alpha") else "internal")
    decision.set_resolvers(groups=lambda p, s: ["team"] if p in ("@bob:alpha", "@cy:alpha") else [],
                           roles=lambda p, s: [])
    try:
        _share(library, item["id"], "group:team", "viewer")
        assert client.get(f"{API}/items/{item['id']}", headers=BOB).status_code == 200
        assert client.get(f"{API}/items/{item['id']}", headers=CY).status_code == 404
        assert item["id"] not in [i["id"] for i in client.get(f"{API}/items", headers=CY).json()["items"]]
    finally:
        gc.set_clearance_resolver(None)
