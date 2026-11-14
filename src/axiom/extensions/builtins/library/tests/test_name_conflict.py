# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Same name on upload: Replace (a new version), Keep both, or Cancel (parity U7, regression 3).

The Library follows a familiar file browser: uploading a file whose name is
already there, with different bytes, asks the person. Replace adds the new
bytes as the next version of the existing file; Keep both files the new one
as ``name (1).ext``; Cancel sends nothing. A client opts in with
``on_conflict``: ``ask`` answers 409 with the existing file, ``replace`` and
``keep_both`` carry the person's answer. Without it an upload behaves as it
always has (the CLI, skills and older clients are unchanged).

Acceptance rows (tagged in each test):

- NC-1: ``ask`` and a same-name file with other bytes: 409 ``name_conflict``,
  naming the existing file and whether the caller may replace it; nothing staged.
- NC-2: ``ask`` and identical bytes: not a conflict (the duplicate path files it once).
- NC-3: ``ask`` and a new name: filed as usual.
- NC-4: ``keep_both``: filed as ``name (1).ext``, then ``name (2).ext``.
- NC-5: ``replace``: the existing item gets a new version with the new bytes;
  one item, two versions, the job follows the same item.
- NC-6: a colleague's file: ``can_replace`` is false and ``replace`` is refused (403).
- NC-7: the multipart route takes the same answers, per file.
- NC-8: no ``on_conflict``: unchanged behaviour (a second item).
- NC-9: the name match ignores case and deleted files.
"""

from __future__ import annotations

import hashlib

import pytest

from axiom.extensions.builtins.library import contrib
from axiom.extensions.builtins.library.extract import registry
from axiom.extensions.builtins.library.extract.routes import clear_routes
from axiom.extensions.builtins.library.tests.test_library_upload import ALICE, ALICE_COLLEAGUE, _up

API = "/api/v1/library"


@pytest.fixture(autouse=True)
def _pipeline(monkeypatch):
    monkeypatch.setenv("AXIOM_LIBRARY_PIPELINE_INLINE", "1")
    contrib.reset(rediscover=False)
    registry.clear_local()
    clear_routes()
    yield
    registry.clear_local()
    clear_routes()
    contrib.reset()


def _start(client, name, data, headers=ALICE, **opts):
    body = {"file_name": name, "size": len(data), "sha256": hashlib.sha256(data).hexdigest(), **opts}
    return client.post(f"{API}/uploads", json=body, headers=headers)


def _send(client, session, data, headers=ALICE):
    r = client.patch(f"{API}/uploads/{session['upload_id']}", content=data, headers={**headers, "Upload-Offset": "0"})
    assert r.status_code == 200, r.text
    return client.get(f"{API}/jobs/{session['job_id']}", headers=headers).json()


def _live(client, headers=ALICE):
    return client.get(f"{API}/items", headers=headers).json()["items"]


def test_nc1_ask_answers_409_with_the_existing_file(client, library):
    first = _up(client, "report.txt", b"version one\n").json()["items"][0]
    jobs_before = client.get(f"{API}/jobs", headers=ALICE).json()["jobs"]
    r = _start(client, "report.txt", b"version two\n", on_conflict="ask")
    assert r.status_code == 409, r.text
    body = r.json()["detail"]
    assert body["code"] == "name_conflict" and body["file_name"] == "report.txt"
    assert body["existing"]["id"] == first["id"] and body["existing"]["file_name"] == "report.txt"
    assert body["can_replace"] is True and body["keep_both_name"] == "report (1).txt"
    assert len(client.get(f"{API}/jobs", headers=ALICE).json()["jobs"]) == len(jobs_before)  # nothing staged


def test_nc2_identical_bytes_are_not_a_conflict(client, library):
    _up(client, "report.txt", b"same\n")
    r = _start(client, "report.txt", b"same\n", on_conflict="ask")
    assert r.status_code == 201, r.text
    job = _send(client, r.json(), b"same\n")
    assert job["state"] == "done" and len(_live(client)) == 1


def test_nc3_a_new_name_is_filed_as_usual(client, library):
    _up(client, "report.txt", b"one\n")
    r = _start(client, "other.txt", b"two\n", on_conflict="ask")
    assert r.status_code == 201, r.text


def test_nc4_keep_both_numbers_the_new_file(client, library):
    _up(client, "report.txt", b"one\n")
    r = _start(client, "report.txt", b"two\n", on_conflict="keep_both")
    assert r.status_code == 201, r.text
    job = _send(client, r.json(), b"two\n")
    names = sorted(i["file_name"] for i in _live(client))
    assert names == ["report (1).txt", "report.txt"], names
    assert job["file_name"] == "report (1).txt"
    r = _start(client, "report.txt", b"three\n", on_conflict="keep_both")
    _send(client, r.json(), b"three\n")
    assert sorted(i["file_name"] for i in _live(client)) == ["report (1).txt", "report (2).txt", "report.txt"]


def test_nc5_replace_adds_a_version_to_the_existing_file(client, library):
    first = _up(client, "report.txt", b"version one\n").json()["items"][0]
    r = _start(client, "report.txt", b"version two\n", on_conflict="replace")
    assert r.status_code == 201, r.text
    job = _send(client, r.json(), b"version two\n")
    assert job["state"] == "done" and job["item_id"] == first["id"], job
    live = _live(client)
    assert [i["id"] for i in live] == [first["id"]]
    versions = client.get(f"{API}/items/{first['id']}/versions", headers=ALICE).json()["versions"]
    assert [v["version_number"] for v in versions] == [2, 1] and versions[0]["current"] is True
    assert client.get(f"{API}/items/{first['id']}/content", headers=ALICE).content == b"version two\n"


def test_nc6_a_colleagues_file_cannot_be_replaced(client, library):
    theirs = _up(client, "report.txt", b"bob's\n", headers=ALICE_COLLEAGUE).json()["items"][0]
    r = _start(client, "report.txt", b"alice's\n", on_conflict="ask")
    assert r.status_code == 409
    assert r.json()["detail"]["can_replace"] is False and r.json()["detail"]["existing"]["id"] == theirs["id"]
    r = _start(client, "report.txt", b"alice's\n", on_conflict="replace")
    assert r.status_code == 403, r.text
    r = _start(client, "report.txt", b"alice's\n", on_conflict="keep_both")
    assert r.status_code == 201 and r.json()["file_name"] == "report (1).txt"


def test_nc7_multipart_takes_the_same_answers(client, library):
    first = _up(client, "report.txt", b"one\n").json()["items"][0]
    r = _up(client, "report.txt", b"two\n", on_conflict="ask")
    assert r.status_code == 409, r.text
    refused = r.json()["refused"][0]
    assert refused["status"] == 409 and refused["conflict"]["existing"]["id"] == first["id"]
    r = _up(client, "report.txt", b"two\n", on_conflict="keep_both")
    assert r.status_code == 201 and r.json()["items"][0]["file_name"] == "report (1).txt"
    r = _up(client, "report.txt", b"three\n", on_conflict="replace")
    assert r.status_code == 201 and r.json()["items"][0]["id"] == first["id"]
    assert client.get(f"{API}/items/{first['id']}/content", headers=ALICE).content == b"three\n"


def test_nc8_without_on_conflict_nothing_changes(client, library):
    _up(client, "report.txt", b"one\n")
    r = _up(client, "report.txt", b"two\n")
    assert r.status_code == 201
    assert [i["file_name"] for i in _live(client)] == ["report.txt", "report.txt"]


def test_nc9_the_match_ignores_case_and_deleted_files(client, library):
    gone = _up(client, "notes.txt", b"old\n").json()["items"][0]
    assert client.delete(f"{API}/items/{gone['id']}", headers=ALICE).status_code == 200
    assert _start(client, "notes.txt", b"new\n", on_conflict="ask").status_code == 201
    _up(client, "Plan.TXT", b"one\n")
    r = _start(client, "plan.txt", b"two\n", on_conflict="ask")
    assert r.status_code == 409 and r.json()["detail"]["existing"]["file_name"] == "Plan.TXT"
