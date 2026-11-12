# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Copy, versions, delete forever and zip: per person, per site, and nothing destroyed by accident."""

from __future__ import annotations

import hashlib
import io
import json
import zipfile

import pytest

from axiom.extensions.builtins.library.tests.test_library_upload import (
    ALICE,
    ALICE_COLLEAGUE,
    DOC,
    MALLORY,
    _up,
)

BASE = "/api/v1/library"
V2 = b"# Pump maintenance\n\nRevised: the seals are now checked monthly.\n"
OTHER = b"# Valve log\n\nThe spring valve was cycled twice and held.\n"


@pytest.fixture(autouse=True)
def _inline_pipeline_and_ledger(monkeypatch, tmp_path):
    monkeypatch.setenv("AXIOM_LIBRARY_PIPELINE_INLINE", "1")
    monkeypatch.setenv("AXIOM_ACTION_LEDGER_BACKEND", "jsonl")
    monkeypatch.setattr(
        "axiom.policy.action_ledger._default_state_dir", lambda _d: tmp_path / "state"
    )
    from axiom.policy import action_ledger

    action_ledger._reset_backend_cache()
    yield tmp_path / "state" / "audit" / "actions.jsonl"
    action_ledger._reset_backend_cache()


def _blob_count(library) -> int:
    return len([p for p in (library["root"] / "alpha" / "_content").rglob("*") if p.is_file()])


# --- copy --------------------------------------------------------------------------


def test_a_copy_is_a_new_item_of_yours_over_the_same_bronze_bytes(client, library):
    item = _up(client, "pump.md", DOC).json()["items"][0]
    blobs = _blob_count(library)
    copy = client.post(f"{BASE}/items/{item['id']}/copy", headers=ALICE_COLLEAGUE).json()
    assert (
        copy["id"] != item["id"]
        and copy["owner"] == "@bob:alpha"
        and copy["sha256"] == item["sha256"]
    )
    assert copy["attributes"]["copied_from"] == {"item_id": item["id"], "sha256": item["sha256"]}
    assert _blob_count(library) == blobs  # nothing written twice
    with library["factory"]() as s:
        from axiom.extensions.builtins.library.db_models import LibraryItem

        a, b = s.get(LibraryItem, item["id"]), s.get(LibraryItem, copy["id"])
        assert a.bronze_content == b.bronze_content and a.source_path != b.source_path
    # the copy is Bob's to change; the original is still Alice's alone
    assert (
        client.patch(
            f"{BASE}/items/{copy['id']}", json={"tags": ["mine"]}, headers=ALICE_COLLEAGUE
        ).status_code
        == 200
    )
    assert client.get(f"{BASE}/items/{copy['id']}/content", headers=ALICE_COLLEAGUE).content == DOC


def test_a_copy_goes_into_the_folder_asked_for(client):
    item = _up(client, "pump.md", DOC).json()["items"][0]
    folder = client.post(f"{BASE}/book/folders", json={"label": "Kept"}, headers=ALICE).json()[
        "path"
    ]
    copy = client.post(
        f"{BASE}/items/{item['id']}/copy", json={"folder": folder}, headers=ALICE
    ).json()
    assert client.get(f"{BASE}/items/{copy['id']}", headers=ALICE).json()["folder"] == folder
    assert (
        client.post(
            f"{BASE}/items/{item['id']}/copy", json={"folder": "no/such"}, headers=ALICE
        ).status_code
        == 404
    )


def test_copy_never_crosses_sites_or_reaches_a_private_file(client):
    item = _up(client, "pump.md", DOC).json()["items"][0]
    private = _up(client, "private.md", OTHER, visibility="private").json()["items"][0]
    assert client.post(f"{BASE}/items/{item['id']}/copy", headers=MALLORY).status_code == 404
    assert (
        client.post(f"{BASE}/items/{private['id']}/copy", headers=ALICE_COLLEAGUE).status_code
        == 404
    )


def test_a_folder_copy_makes_the_folder_again_with_copies_of_what_is_in_it(client):
    top = client.post(f"{BASE}/book/folders", json={"label": "Runs"}, headers=ALICE).json()["path"]
    sub = client.post(
        f"{BASE}/book/folders", json={"label": "April", "parent": top}, headers=ALICE
    ).json()["path"]
    a = _up(client, "pump.md", DOC, folder=top).json()["items"][0]
    b = _up(client, "valve.md", OTHER, folder=sub).json()["items"][0]
    assert {a["folder"], b["folder"]} == {top, sub}
    out = client.post(f"{BASE}/book/folders/{top}/copy", headers=ALICE).json()
    assert out["copied"] == 2 and out["folders"] == 2 and out["path"] != top
    copied = client.get(
        f"{BASE}/items", params={"folder": out["path"], "below": True}, headers=ALICE
    ).json()["items"]
    assert sorted(i["file_name"] for i in copied) == ["pump.md", "valve.md"]
    assert client.post(f"{BASE}/book/folders/{top}/copy", headers=MALLORY).status_code == 404


# --- versions ----------------------------------------------------------------------


def _new_version(client, item_id, data, name="pump.md", headers=ALICE):
    return client.post(
        f"{BASE}/items/{item_id}/versions", files=[("files", (name, data))], headers=headers
    )


def test_a_new_version_keeps_the_old_bytes_and_its_own_results(client):
    item = _up(client, "pump.md", DOC).json()["items"][0]
    assert [
        v["version_number"]
        for v in client.get(f"{BASE}/items/{item['id']}/versions", headers=ALICE).json()["versions"]
    ] == [1]
    added = _new_version(client, item["id"], V2)
    assert added.status_code == 201 and added.json()["version_number"] == 2
    versions = client.get(f"{BASE}/items/{item['id']}/versions", headers=ALICE).json()["versions"]
    assert [(v["version_number"], v["current"]) for v in versions] == [(2, True), (1, False)]
    assert (
        versions[0]["sha256"] == hashlib.sha256(V2).hexdigest()
        and versions[0]["job_id"] != versions[1]["job_id"]
    )
    assert client.get(f"{BASE}/items/{item['id']}/content", headers=ALICE).content == V2
    assert client.get(versions[1]["download_href"], headers=ALICE).content == DOC
    assert client.get(versions[0]["download_href"], headers=ALICE).content == V2


def test_restoring_makes_a_new_version_and_destroys_nothing(client):
    item = _up(client, "pump.md", DOC).json()["items"][0]
    _new_version(client, item["id"], V2)
    out = client.post(f"{BASE}/items/{item['id']}/versions/1/restore", headers=ALICE).json()
    assert out == {**out, "version_number": 3, "restored_from": 1}
    versions = client.get(f"{BASE}/items/{item['id']}/versions", headers=ALICE).json()["versions"]
    assert [(v["version_number"], v["source"], v["restored_from"]) for v in versions] == [
        (3, "restore", 1),
        (2, "version", None),
        (1, "upload", None),
    ]
    assert client.get(f"{BASE}/items/{item['id']}/content", headers=ALICE).content == DOC
    assert client.get(versions[1]["download_href"], headers=ALICE).content == V2
    assert (
        client.post(f"{BASE}/items/{item['id']}/versions/3/restore", headers=ALICE).status_code
        == 422
    )


def test_versions_follow_the_items_scope(client):
    item = _up(client, "pump.md", DOC).json()["items"][0]
    # a colleague reads and downloads versions but does not add or restore them
    assert (
        client.get(f"{BASE}/items/{item['id']}/versions", headers=ALICE_COLLEAGUE).status_code
        == 200
    )
    assert _new_version(client, item["id"], V2, headers=ALICE_COLLEAGUE).status_code == 403
    _new_version(client, item["id"], V2)
    assert (
        client.post(
            f"{BASE}/items/{item['id']}/versions/1/restore", headers=ALICE_COLLEAGUE
        ).status_code
        == 403
    )
    # another site learns nothing
    assert client.get(f"{BASE}/items/{item['id']}/versions", headers=MALLORY).status_code == 404
    assert (
        client.get(f"{BASE}/items/{item['id']}/versions/1/content", headers=MALLORY).status_code
        == 404
    )
    assert _new_version(client, item["id"], V2, headers=MALLORY).status_code == 404


# --- delete forever ----------------------------------------------------------------


def _ledger_rows(path):
    """The action ledger's records. The skill audit chain shares the file (a
    delete is recorded there, MVP-22-A2) and its records have no ``op_class``."""
    if not path.exists():
        return []
    return [r for r in (json.loads(line) for line in path.read_text().splitlines()) if "op_class" in r]


def test_delete_forever_only_from_deleted_files_by_its_owner_and_it_is_audited(
    client, library, _inline_pipeline_and_ledger
):
    item = _up(client, "pump.md", DOC).json()["items"][0]
    assert (
        client.post(f"{BASE}/items/{item['id']}/purge", headers=ALICE).status_code == 404
    )  # not deleted yet
    client.delete(f"{BASE}/items/{item['id']}", headers=ALICE)
    assert (
        client.post(f"{BASE}/items/{item['id']}/purge", headers=ALICE_COLLEAGUE).status_code == 404
    )
    assert client.post(f"{BASE}/items/{item['id']}/purge", headers=MALLORY).status_code == 404
    blobs = _blob_count(library)
    out = client.post(f"{BASE}/items/{item['id']}/purge", headers=ALICE).json()
    assert out["purged"] and out["bytes_removed"] == 1
    assert _blob_count(library) == blobs - 1
    assert (
        client.get(f"{BASE}/items", params={"deleted": True}, headers=ALICE).json()["items"] == []
    )
    rows = _ledger_rows(_inline_pipeline_and_ledger)
    # the colleague's refused attempt is on the record too
    assert [(r["op_class"], r["candidate"], r["outcome"], r["agent"]) for r in rows] == [
        ("library.purge", item["id"], "refused", "@bob:alpha"),
        ("library.purge", item["id"], "proceeded", "@alice:alpha"),
    ]
    with library["factory"]() as s:
        from axiom.extensions.builtins.library.db_models import LibraryEvent

        assert "purge" in [
            e.action for e in s.query(LibraryEvent).filter(LibraryEvent.item_id == item["id"])
        ]


def test_delete_forever_keeps_bytes_a_copy_still_uses(client, library):
    item = _up(client, "pump.md", DOC).json()["items"][0]
    copy = client.post(f"{BASE}/items/{item['id']}/copy", headers=ALICE).json()
    client.delete(f"{BASE}/items/{item['id']}", headers=ALICE)
    out = client.post(f"{BASE}/items/{item['id']}/purge", headers=ALICE).json()
    assert out["bytes_removed"] == 0 and out["bytes_kept_shared"] == 1
    assert client.get(f"{BASE}/items/{copy['id']}/content", headers=ALICE).content == DOC


def test_delete_forever_removes_every_version(client, library):
    item = _up(client, "pump.md", DOC).json()["items"][0]
    _new_version(client, item["id"], V2)
    client.delete(f"{BASE}/items/{item['id']}", headers=ALICE)
    assert (
        client.post(f"{BASE}/items/{item['id']}/purge", headers=ALICE).json()["bytes_removed"] == 2
    )


def test_an_export_control_hold_refuses_delete_forever_and_says_so(
    client, library, _inline_pipeline_and_ledger
):
    item = _up(client, "pump.md", DOC).json()["items"][0]
    with library["factory"]() as s:
        from axiom.extensions.builtins.library.db_models import LibraryItem

        s.get(LibraryItem, item["id"]).ec = {"verdict": "held", "reasons": ["test"]}
        s.commit()
    client.delete(f"{BASE}/items/{item['id']}", headers=ALICE)
    refused = client.post(f"{BASE}/items/{item['id']}/purge", headers=ALICE)
    assert refused.status_code == 423 and "export-control hold" in refused.json()["detail"]
    assert [r["outcome"] for r in _ledger_rows(_inline_pipeline_and_ledger)] == ["refused"]
    assert client.get(f"{BASE}/items", params={"deleted": True}, headers=ALICE).json()["items"]


# --- zip ---------------------------------------------------------------------------


def test_zip_streams_the_selection_with_folders_as_directories(client):
    top = client.post(f"{BASE}/book/folders", json={"label": "Runs"}, headers=ALICE).json()["path"]
    a = _up(client, "pump.md", DOC).json()["items"][0]
    _up(client, "valve.md", OTHER, folder=top)
    got = client.post(f"{BASE}/zip", json={"items": [a["id"]], "folders": [top]}, headers=ALICE)
    assert got.status_code == 200 and got.headers["content-type"] == "application/zip"
    z = zipfile.ZipFile(io.BytesIO(got.content))
    assert sorted(z.namelist()) == ["Runs/valve.md", "pump.md"]
    assert z.read("pump.md") == DOC and z.read("Runs/valve.md") == OTHER


def test_zip_from_a_form_post_and_its_size_check(client):
    a = _up(client, "pump.md", DOC).json()["items"][0]
    check = client.post(
        f"{BASE}/zip", params={"check": True}, data={"items": a["id"]}, headers=ALICE
    ).json()
    assert check["files"] == 1 and check["bytes"] == len(DOC)
    got = client.post(f"{BASE}/zip", data={"items": a["id"]}, headers=ALICE)
    assert zipfile.ZipFile(io.BytesIO(got.content)).read("pump.md") == DOC


def test_zip_over_the_cap_is_a_clear_413_before_anything_is_sent(client, monkeypatch):
    a = _up(client, "pump.md", DOC).json()["items"][0]
    monkeypatch.setenv("AXIOM_LIBRARY_MAX_ZIP_BYTES", "10")
    refused = client.post(f"{BASE}/zip", json={"items": [a["id"]]}, headers=ALICE)
    assert refused.status_code == 413 and "at most" in refused.json()["detail"]


def test_zip_never_crosses_sites_or_includes_what_you_cannot_read(client):
    top = client.post(f"{BASE}/book/folders", json={"label": "Runs"}, headers=ALICE).json()["path"]
    a = _up(client, "pump.md", DOC).json()["items"][0]
    _up(client, "private.md", OTHER, folder=top, visibility="private")
    assert client.post(f"{BASE}/zip", json={"items": [a["id"]]}, headers=MALLORY).status_code == 404
    got = client.post(f"{BASE}/zip", json={"folders": [top]}, headers=ALICE_COLLEAGUE)
    assert zipfile.ZipFile(io.BytesIO(got.content)).namelist() == []


def test_the_zip_writer_holds_one_chunk_at_a_time(library):
    from axiom.extensions.builtins.library import operations

    big = library["root"] / "big.bin"
    big.parent.mkdir(parents=True, exist_ok=True)
    big.write_bytes(b"\0" * (3 * operations.ZIP_CHUNK + 5))
    pieces = list(operations.zip_stream([("big.bin", "big.bin", big.stat().st_size)]))
    assert len(pieces) > 3  # streamed in pieces, not built whole
    assert zipfile.ZipFile(io.BytesIO(b"".join(pieces))).read("big.bin") == big.read_bytes()


# --- paging (the browser loads 10,000 files a page at a time) ------------------------


def test_items_page_with_a_cursor_and_sort(client):
    for n in range(5):
        _up(client, f"note-{n}.md", f"# Note {n}\n\nbody {n}\n".encode())
    first = client.get(
        f"{BASE}/items", params={"limit": 2, "sort_by": "name", "sort_dir": "asc"}, headers=ALICE
    ).json()
    assert [i["file_name"] for i in first["items"]] == ["note-0.md", "note-1.md"] and first[
        "next_cursor"
    ]
    second = client.get(
        f"{BASE}/items",
        params={"limit": 2, "sort_by": "name", "sort_dir": "asc", "cursor": first["next_cursor"]},
        headers=ALICE,
    ).json()
    assert [i["file_name"] for i in second["items"]] == ["note-2.md", "note-3.md"]
    last = client.get(
        f"{BASE}/items",
        params={"limit": 2, "sort_by": "name", "sort_dir": "asc", "cursor": second["next_cursor"]},
        headers=ALICE,
    ).json()
    assert [i["file_name"] for i in last["items"]] == ["note-4.md"] and last["next_cursor"] is None
    assert client.get(f"{BASE}/items", headers=MALLORY).json()["items"] == []
