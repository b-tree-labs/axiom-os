# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Tidy up: propose a reorganisation, review it, apply it (parity F26, regression 6).

The earlier product the Library came from had a "Tidy up" button: it proposed
moving files to where they belong and removing exact duplicates, the person
ticked what to accept, and only the accepted items changed. A move made that
way is a person's move, so the Book learns from it. Proposals are
deterministic: the same Library proposes the same list.

Acceptance rows (tagged in each test):

- TU-1: exact copies: the later copies are proposed for Deleted files, the
  earliest is kept; proposing changes nothing.
- TU-2: a file the Book would now file elsewhere (a person moved a similar one)
  is proposed to move there, with the reason.
- TU-3: a file a person placed is never proposed to move.
- TU-4: only the caller's own files are proposed, and apply refuses anyone else's.
- TU-5: apply changes only what was accepted; a move is recorded as the
  person's (the Book learns), a duplicate goes to Deleted files and can be restored.
- TU-6: apply never removes the last copy: a duplicate whose kept copy is gone is skipped.
- TU-7: the same Library proposes the same list.
- TU-8: a move the Book only inferred (from one person's move, or a folder's
  resemblance) is proposed below high confidence, so the review starts it unticked.
"""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.library import contrib
from axiom.extensions.builtins.library.extract import registry
from axiom.extensions.builtins.library.tests.test_library_upload import ALICE, ALICE_COLLEAGUE, _up

API = "/api/v1/library"
PROSE = "\n\nThis paragraph is long enough that the ingestion guard treats it as prose and indexes it for retrieval.\n"


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    monkeypatch.setenv("AXIOM_LIBRARY_PIPELINE_INLINE", "1")
    contrib.reset(rediscover=False)
    registry.clear_local()
    yield
    registry.clear_local()
    contrib.reset()


def _doc(client, name, heading, headers=ALICE):
    r = _up(client, name, (f"# {heading}" + PROSE).encode(), headers=headers)
    assert r.status_code == 201, r.text
    return r.json()["items"][0]


def _propose(client, headers=ALICE, **body):
    r = client.post(f"{API}/tidy/proposals", json=body, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def _by_item(proposals):
    return {p["item_id"]: p for p in proposals["proposals"]}


def _calibration_setup(client):
    first = _doc(client, "pump-calibration-2026.md", "Pump calibration")
    second = _doc(client, "valve-calibration-2026.md", "Valve calibration")
    assert first["folder"] == second["folder"] == "documents"
    folder = client.post(f"{API}/book/folders", json={"label": "Calibration"}, headers=ALICE).json()
    client.post(f"{API}/items/{first['id']}/move", json={"folder": folder["path"]}, headers=ALICE)
    return first, second, folder["path"]


def test_tu1_copies_are_proposed_for_deleted_files_keeping_the_earliest(client, library):
    original = _doc(client, "plan.md", "Plan")
    copy = client.post(f"{API}/items/{original['id']}/copy", json={}, headers=ALICE).json()
    before = client.get(f"{API}/items", headers=ALICE).json()["items"]
    found = _by_item(_propose(client))
    assert copy["id"] in found and original["id"] not in found
    p = found[copy["id"]]
    assert p["action"] == "delete_duplicate" and p["duplicate_of"]["id"] == original["id"]
    assert "same contents" in p["because"]
    assert client.get(f"{API}/items", headers=ALICE).json()["items"] == before  # proposing changes nothing


def test_tu2_a_file_the_book_would_now_file_elsewhere_is_proposed_to_move(client, library):
    _first, second, path = _calibration_setup(client)
    p = _by_item(_propose(client))[second["id"]]
    assert p["action"] == "move" and p["from_folder"] == "documents" and p["to_folder"] == path
    assert p["to_labels"][-1] == "Calibration" and p["because"]


def test_tu3_a_file_a_person_placed_is_never_moved(client, library):
    first, _second, _path = _calibration_setup(client)
    assert first["id"] not in _by_item(_propose(client))


def test_tu4_only_the_callers_files(client, library):
    _first, second, path = _calibration_setup(client)
    theirs = _doc(client, "seal-calibration-2026.md", "Seal calibration", headers=ALICE_COLLEAGUE)
    found = _by_item(_propose(client))
    assert theirs["id"] not in found and second["id"] in found
    where = client.get(f"{API}/items/{theirs['id']}", headers=ALICE).json()["folder"]
    r = client.post(f"{API}/tidy/apply", json={"items": [{"item_id": theirs["id"], "action": "move", "to_folder": "code"}]},
                    headers=ALICE)
    assert r.status_code == 200
    assert r.json()["applied"] == [] and r.json()["skipped"][0]["item_id"] == theirs["id"]
    assert client.get(f"{API}/items/{theirs['id']}", headers=ALICE).json()["folder"] == where


def test_tu5_apply_changes_only_what_was_accepted(client, library):
    _first, second, path = _calibration_setup(client)
    original = _doc(client, "notes.md", "Notes")
    copy = client.post(f"{API}/items/{original['id']}/copy", json={}, headers=ALICE).json()
    other = _doc(client, "readme.md", "Readme")
    copy2 = client.post(f"{API}/items/{other['id']}/copy", json={}, headers=ALICE).json()
    found = _by_item(_propose(client))
    assert {second["id"], copy["id"], copy2["id"]} <= set(found)
    accepted = [found[second["id"]], found[copy["id"]]]  # copy2 left unticked
    r = client.post(f"{API}/tidy/apply", json={"items": [
        {"item_id": p["item_id"], "action": p["action"], "to_folder": p.get("to_folder")} for p in accepted
    ]}, headers=ALICE)
    assert r.status_code == 200, r.text
    assert sorted(a["item_id"] for a in r.json()["applied"]) == sorted([second["id"], copy["id"]])
    moved = client.get(f"{API}/items/{second['id']}", headers=ALICE).json()
    assert moved["folder"] == path and moved["placement"]["source"] == "manual"
    assert client.get(f"{API}/items/{copy['id']}", headers=ALICE).status_code == 404
    assert client.post(f"{API}/items/{copy['id']}/restore", headers=ALICE).status_code == 200
    assert client.get(f"{API}/items/{copy2['id']}", headers=ALICE).status_code == 200  # not accepted, not touched


def test_tu6_the_last_copy_is_never_removed(client, library):
    original = _doc(client, "plan.md", "Plan")
    copy = client.post(f"{API}/items/{original['id']}/copy", json={}, headers=ALICE).json()
    client.delete(f"{API}/items/{original['id']}", headers=ALICE)
    r = client.post(f"{API}/tidy/apply", json={"items": [{"item_id": copy["id"], "action": "delete_duplicate"}]},
                    headers=ALICE)
    assert r.json()["applied"] == [] and "no other copy" in r.json()["skipped"][0]["reason"]
    assert client.get(f"{API}/items/{copy['id']}", headers=ALICE).status_code == 200


def test_tu7_the_same_library_proposes_the_same_list(client, library):
    _calibration_setup(client)
    original = _doc(client, "plan.md", "Plan")
    client.post(f"{API}/items/{original['id']}/copy", json={}, headers=ALICE)
    assert _propose(client) == _propose(client)


def test_tu8_a_move_the_book_only_inferred_is_not_high_confidence(client, library):
    """One person's move, or a folder's resemblance, is a guess: proposed, but never ticked by default."""
    _first, second, _path = _calibration_setup(client)
    p = _by_item(_propose(client))[second["id"]]
    assert p["confidence"] < 0.8, p
    original = _doc(client, "plan.md", "Plan")
    copy = client.post(f"{API}/items/{original['id']}/copy", json={}, headers=ALICE).json()
    assert _by_item(_propose(client))[copy["id"]]["confidence"] == 1.0  # same bytes is a fact
