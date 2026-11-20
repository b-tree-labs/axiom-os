# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Before a move widens who can see a file, the person is told (prd-sharing-and-access SHR-R4.3).

"Moving it here lets 2 more people see it": a move into a person's folder that is
shared further than the file is asks first; with the person's yes it goes ahead.
"""

from __future__ import annotations

from axiom.extensions.builtins.library.tests.test_library_upload import _up

API = "/api/v1/library"
ALICE = {"x-test-principal": "@alice:alpha", "x-test-site": "alpha"}
PROSE = "\n\nNotes kept for the team, about the quarterly review and what it found.\n"


def _item(client, **fields):
    r = _up(client, "notes.md", ("# notes" + PROSE).encode(), headers=ALICE, **fields)
    assert r.status_code == 201, r.text
    return r.json()["items"][0]


def _folder(client, label):
    return client.post(f"{API}/book/folders", json={"label": label}, headers=ALICE).json()["path"]


def _folder_obj(library, path):
    from axiom.extensions.builtins.library import access

    with library["factory"]() as s:
        folders = access.user_folders(s, "alpha")
        return access.folder_object(folders[path], folders)


def _share_folder(library, path, who):
    from axiom.governance.access import decision, sharing

    sharing.share(decision.who("@alice:alpha", "alpha"), _folder_obj(library, path), who, "viewer")


def test_shr_r4_3_a_move_that_widens_access_asks_first_and_names_how_many(client, library):
    item = _item(client, visibility="private")
    path = _folder(client, "Team")
    _share_folder(library, path, "@cy:alpha")
    _share_folder(library, path, "group:readers")
    asked = client.post(f"{API}/items/{item['id']}/move", json={"folder": path}, headers=ALICE)
    assert asked.status_code == 409, asked.text
    detail = asked.json()["detail"]
    assert detail["code"] == "widens_access" and detail["widens"]["people"] == ["@cy:alpha", "group:readers"]
    assert detail["message"] == "Moving it here lets 2 more people or groups see it."
    with library["factory"]() as s:
        from axiom.extensions.builtins.library.db_models import LibraryItem

        assert s.get(LibraryItem, item["id"]).folder != path  # nothing moved
    done = client.post(f"{API}/items/{item['id']}/move", json={"folder": path, "confirm": True}, headers=ALICE)
    assert done.status_code == 200 and done.json()["folder"] == path


def test_a_folder_open_to_the_site_says_everyone_at_the_site(client, library):
    item = _item(client, visibility="private")
    path = _folder(client, "Open")
    from axiom.governance.access import decision, sharing

    sharing.set_general(decision.who("@alice:alpha", "alpha"), _folder_obj(library, path), "site", "viewer")
    asked = client.post(f"{API}/items/{item['id']}/move", json={"folder": path}, headers=ALICE)
    assert asked.status_code == 409 and asked.json()["detail"]["message"] == "Moving it here lets everyone at alpha see it."


def test_a_move_that_widens_nothing_goes_ahead_at_once(client, library):
    item = _item(client)  # already everyone at the site
    path = _folder(client, "Mine")
    _share_folder(library, path, "@cy:alpha")
    assert client.post(f"{API}/items/{item['id']}/move", json={"folder": path}, headers=ALICE).status_code == 200
    other = _item(client, visibility="private")
    assert client.post(f"{API}/items/{other['id']}/move", json={"folder": "documents"}, headers=ALICE).status_code == 200


def test_the_skill_asks_too_and_takes_confirm(client, library, tmp_path):
    import logging

    from axiom.extensions.builtins.library.skills import bind_default
    from axiom.infra.principal import PrincipalContext
    from axiom.infra.skills import SkillContext

    item = _item(client, visibility="private")
    path = _folder(client, "Crew")
    _share_folder(library, path, "@cy:alpha")
    reg = bind_default()
    ctx = SkillContext(registry=reg, state_dir=tmp_path, logger=logging.getLogger("t"),
                       principal=PrincipalContext(handle="@alice:alpha"), surface="mcp")
    asked = reg.invoke("library.move", {"id": item["id"], "folder": path}, ctx)
    assert not asked.ok and "1 more person or group" in asked.errors[0] and "confirm" in asked.errors[0]
    assert reg.invoke("library.move", {"id": item["id"], "folder": path, "confirm": True}, ctx).ok
