# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""When a person leaves a site, their files and folders go to a named person (SHR-R5.3, SHR-A10).

A site-admin or an access admin runs it, through this one step only; no second
approval (decided by the owner). Nothing is left without an Owner, nobody loses
access they had through those files, every transfer is recorded and the new
Owner is told. Running it gives the administrator no access to any of it.
"""

from __future__ import annotations

import logging

import pytest

from axiom.extensions.builtins.library import sharing as lib_sharing
from axiom.extensions.builtins.library.db_models import LibraryEvent, LibraryFolder, LibraryItem
from axiom.extensions.builtins.library.tests.test_library_upload import _up
from axiom.governance.access import decision

API = "/api/v1/library"
ALICE = {"x-test-principal": "@alice:alpha", "x-test-site": "alpha"}
BOB = {"x-test-principal": "@bob:alpha", "x-test-site": "alpha"}
CY = {"x-test-principal": "@cy:alpha", "x-test-site": "alpha"}
SAM = {"x-test-principal": "@sam:alpha", "x-test-site": "alpha"}
PROSE = "\n\nNotes kept for the team, about the quarterly review and what it found.\n"


@pytest.fixture
def roles(library):
    held = {"@sam:alpha": ["site-admin"], "@ada:alpha": ["admin"]}
    decision.set_resolvers(groups=lambda p, s: [], roles=lambda p, s: held.get(p, []))
    yield
    decision.set_resolvers(groups=lambda p, s: [], roles=lambda p, s: [])


@pytest.fixture
def told(library):
    sent: list[tuple[str, str]] = []
    lib_sharing.set_notifier(lambda to, summary, body, dedup: sent.append((to, summary)))
    yield sent
    lib_sharing.set_notifier(None)


@pytest.fixture
def skills(library, tmp_path):
    from axiom.extensions.builtins.library.skills import bind_default
    from axiom.infra.principal import PrincipalContext
    from axiom.infra.skills import SkillContext

    reg = bind_default()
    return lambda handle, **p: reg.invoke("library.offboard", p, SkillContext(
        registry=reg, state_dir=tmp_path, logger=logging.getLogger("t"), principal=PrincipalContext(handle=handle),
        surface="cli"))


def _alice_s_things(client):
    path = client.post(f"{API}/book/folders", json={"label": "Alice's"}, headers=ALICE).json()["path"]
    private = _up(client, "private.md", ("# private" + PROSE).encode(), headers=ALICE, visibility="private",
                  folder=path).json()["items"][0]
    shared = _up(client, "shared.md", ("# shared" + PROSE).encode(), headers=ALICE, visibility="private").json()["items"][0]
    r = client.post(f"{API}/items/{shared['id']}/share", json={"with": "@bob:alpha", "role": "viewer", "notify": False},
                    headers=ALICE)
    assert r.status_code == 200, r.text
    return path, private, shared


def test_shr_r5_3_a_site_admin_hands_everything_to_a_named_person(client, library, roles, told, skills):
    path, private, shared = _alice_s_things(client)
    out = skills("@sam:alpha", person="@alice:alpha", to="@cy:alpha")
    assert out.ok, out.errors
    assert out.value["files"] == 2 and out.value["folders"] == 1
    with library["factory"]() as s:
        assert {s.get(LibraryItem, i["id"]).owner for i in (private, shared)} == {"@cy:alpha"}
        assert s.query(LibraryFolder).filter_by(path=path).one().created_by == "@cy:alpha"
        words = [e.detail["words"] for e in s.query(LibraryEvent).filter_by(item_id=shared["id"], action="transfer")]
    assert words == ["@sam:alpha handed @alice:alpha's file to @cy:alpha (offboarding)"]
    # The new Owner opens them; nobody lost access they had through them; the Owner is told.
    assert client.get(f"{API}/items/{private['id']}/content", headers=CY).status_code == 200
    assert client.get(f"{API}/items/{shared['id']}", headers=BOB).status_code == 200
    assert told and told[-1][0] == "@cy:alpha" and "@alice:alpha" in told[-1][1]


def test_shr_a10_running_it_gives_the_administrator_nothing(client, library, roles, skills):
    _path, private, _shared = _alice_s_things(client)
    assert skills("@sam:alpha", person="@alice:alpha", to="@cy:alpha").ok
    assert client.get(f"{API}/items/{private['id']}", headers=SAM).status_code == 404


def test_an_access_admin_may_run_it_and_nobody_else(client, library, roles, skills):
    _alice_s_things(client)
    refused = skills("@bob:alpha", person="@alice:alpha", to="@cy:alpha")
    assert not refused.ok and "site admin" in refused.errors[0].lower()
    assert skills("@ada:alpha", person="@alice:alpha", to="@cy:alpha").ok


def test_handing_to_the_person_who_leaves_is_refused(client, library, roles, skills):
    _alice_s_things(client)
    out = skills("@sam:alpha", person="@alice:alpha", to="@alice:alpha")
    assert not out.ok and "someone else" in out.errors[0]
