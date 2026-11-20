# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""KEEP owns access expiry (prd-sharing-and-access SHR-R7.1, SHR-A6; ADR-209 D8a).

One reminder, a week before, to the person who gave the access; the entry ends
on its day and its end is in the item's activity.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from axiom.extensions.builtins.library import sharing as lib_sharing
from axiom.extensions.builtins.library.db_models import LibraryEvent
from axiom.extensions.builtins.library.tests.test_library_upload import _up
from axiom.governance.access import store

API = "/api/v1/library"
ALICE = {"x-test-principal": "@alice:alpha", "x-test-site": "alpha"}
CY = {"x-test-principal": "@cy:alpha", "x-test-site": "alpha"}


@pytest.fixture
def told(library):
    sent: list[tuple[str, str]] = []
    lib_sharing.set_notifier(lambda to, summary, body, dedup: sent.append((to, summary)))
    yield sent
    lib_sharing.set_notifier(None)


def _shared_until(client, days: int):
    item = _up(client, "notes.md", b"# Notes\n\nKept for the review.\n", headers=ALICE, visibility="private").json()["items"][0]
    until = (store.now() + timedelta(days=days)).date().isoformat()
    r = client.post(f"{API}/items/{item['id']}/share",
                    json={"with": "@cy:alpha", "role": "viewer", "expires": until, "notify": False}, headers=ALICE)
    assert r.status_code == 200, r.text
    return item


def test_shr_a6_one_reminder_a_week_before_to_the_person_who_gave_it(client, library, told):
    from axiom.extensions.builtins.library import expiry

    _shared_until(client, 5)
    first = expiry.tick()
    assert first["reminded"] == 1 and first["ended"] == 0
    assert len(told) == 1 and told[0][0] == "@alice:alpha"
    assert "@cy:alpha" in told[0][1] and "notes.md" in told[0][1] and "ends on" in told[0][1]
    assert expiry.tick()["reminded"] == 0  # one reminder, not one an hour


def test_a_grant_further_off_is_not_reminded_yet(client, library, told):
    from axiom.extensions.builtins.library import expiry

    _shared_until(client, 30)
    assert expiry.tick()["reminded"] == 0 and told == []


def test_shr_a6_an_entry_ends_on_its_day_and_its_end_is_in_the_activity(client, library, told):
    from axiom.extensions.builtins.library import expiry

    item = _shared_until(client, 2)
    assert client.get(f"{API}/items/{item['id']}", headers=CY).status_code == 200
    out = expiry.tick(now=store.now() + timedelta(days=3))
    assert out["ended"] == 1
    assert client.get(f"{API}/items/{item['id']}", headers=CY).status_code == 404
    with library["factory"]() as s:
        ends = [e for e in s.query(LibraryEvent).filter_by(item_id=item["id"], action="expire")]
    assert [e.detail["words"] for e in ends] == ["@cy:alpha's access ended on its day"]
    assert [e.principal for e in ends] == ["@keep"]


def test_keep_declares_the_tick_on_a_schedule():
    import tomllib
    from pathlib import Path

    from axiom.extensions.builtins import library

    manifest = tomllib.loads((Path(library.__file__).parent / "axiom-extension.toml").read_text(encoding="utf-8"))
    ticks = [s for s in manifest["extension"].get("schedule", []) if s["action"] == "library.access_expiry"]
    assert ticks and ticks[0]["cadence"]["kind"] == "interval"
