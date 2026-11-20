# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The share dialog's field takes a name or an email, as Google's does (decided 2026-11-16, routine).

A person signs in through the gate and the node knows them by the gate's handle
(`@<name>:<context>`), which nobody types. So the node finds people at the
caller's site by name or email, a share names a person by email, and the gate
says the signed-in person's own handle.
"""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.library.tests.test_library_upload import _up

API = "/api/v1/library"
ALICE = {"x-test-principal": "@alice:alpha", "x-test-site": "alpha"}
PROSE = "\n\nNotes kept for the team, about the quarterly review and what it found.\n"


@pytest.fixture
def people(tmp_path, monkeypatch):
    from axiom.webauth import upsert_user_record
    from axiom.webauth.password import get_password_hash

    accounts = tmp_path / "gate-users.json"
    monkeypatch.setenv("AXIOM_GATE_USERS_FILE", str(accounts))
    monkeypatch.setenv("AXIOM_SERVED_SITES", "alpha,beta")
    pw = get_password_hash("Correct-Horse-9")
    upsert_user_record(accounts, email="dana.ross@example.org", password_hash=pw, name="Dana Ross", site="alpha")
    upsert_user_record(accounts, email="dan.lee@example.org", password_hash=pw, name="Dan Lee", site="alpha")
    upsert_user_record(accounts, email="dave@example.org", password_hash=pw, name="Dave Beta", site="beta")
    from axiom.webauth import live

    live._reset_for_tests()
    from axiom.extensions.builtins.actors.directory import person_handle

    return {e: person_handle(u, e, s) for e, u, s in (
        ("dana.ross@example.org", "dana.ross@example.org", "alpha"),
        ("dan.lee@example.org", "dan.lee@example.org", "alpha"),
    )}


def _item(client, name="notes.md"):
    r = _up(client, name, ("# " + name + PROSE).encode(), headers=ALICE, visibility="private")
    assert r.status_code == 201, r.text
    return r.json()["items"][0]


def test_people_at_the_site_are_found_by_name_or_email_and_nobody_else(client, people):
    found = client.get(f"{API}/people", params={"q": "dan"}, headers=ALICE).json()["people"]
    assert {p["name"] for p in found} == {"Dana Ross", "Dan Lee"}  # Dave is at another site
    assert all(p["handle"].startswith("@") for p in found)
    by_email = client.get(f"{API}/people", params={"q": "lee@example"}, headers=ALICE).json()["people"]
    assert [p["name"] for p in by_email] == ["Dan Lee"]
    assert client.get(f"{API}/people", params={"q": "d"}, headers=ALICE).status_code == 422  # two letters at least


def test_a_share_names_a_person_by_email(client, people):
    item = _item(client)
    r = client.post(f"{API}/items/{item['id']}/share", json={"with": "dana.ross@example.org", "notify": False},
                    headers=ALICE)
    assert r.status_code == 200, r.text
    dana = {"x-test-principal": people["dana.ross@example.org"], "x-test-site": "alpha"}
    assert client.get(f"{API}/items/{item['id']}", headers=dana).status_code == 200
    unknown = client.post(f"{API}/items/{item['id']}/share", json={"with": "nobody@example.org"}, headers=ALICE)
    assert unknown.status_code in (403, 422) and "Nobody at this site" in unknown.json()["detail"]
    other_site = client.post(f"{API}/items/{item['id']}/share", json={"with": "dave@example.org"}, headers=ALICE)
    assert other_site.status_code in (403, 422)


def test_the_gate_says_the_signed_in_person_s_own_handle():
    from axiom.extensions.builtins.actors.directory import person_handle
    from axiom.extensions.builtins.webgate.api.routers import _user_public

    claims = {"sub": "dana.ross@example.org", "email": "dana.ross@example.org", "site": "alpha", "roles": []}
    assert _user_public(claims)["handle"] == person_handle("dana.ross@example.org", "dana.ross@example.org", "alpha")
