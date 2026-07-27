# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for the student-side materials sync client.

Phase 3 of the materials-flow tier. After the student joins and has
the coordinator's public key, they fetch the signed manifest, verify
it, then download each referenced file verifying content-hash as they
go. The result lives at ``~/.axi/classrooms/<id>/materials/`` so the
chat surface (Phase 6) can retrieve grounded answers from it.

Key invariants:
- Manifest signature MUST verify against the coordinator's pubkey
  before any file is downloaded.
- Every downloaded file's sha256 MUST match the manifest's
  ``content_hash`` before it's persisted.
- A second sync with the same manifest is a no-op (fully cached).
- Re-sync after the coordinator adds new files picks up only the
  new ones (incremental).
- Tampered content during download raises; the partially-downloaded
  state is surfaced, not silently accepted.
"""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.classroom.classroom_materials import (
    ClassroomMaterialsStore,
)
from axiom.extensions.builtins.classroom.materials_manifest import (
    build_materials_manifest,
    encode_materials_manifest,
)
from axiom.extensions.builtins.classroom.materials_sync import (
    InProcessGetTransport,
    MaterialsSyncClient,
    MaterialsTamperError,
    StudentMaterialsStore,
)
from axiom.vega.federation.identity import generate_identity


@pytest.fixture
def coordinator_with_materials(tmp_path):
    """Build a coordinator identity + populated materials store."""
    coord = generate_identity(
        owner="prof@ut.edu", keys_dir=tmp_path / "coord-keys"
    )
    store = ClassroomMaterialsStore(tmp_path / "coord-materials")
    e1 = store.add_text("Fission splits heavy nuclei.", filename="ch1.md")
    e2 = store.add_text("Fusion combines light nuclei.", filename="ch2.md")
    return {
        "coord": coord,
        "store": store,
        "entries": [e1, e2],
    }


# ---------------------------------------------------------------------------
# Student materials store (local cache)
# ---------------------------------------------------------------------------


class TestStudentStore:
    def test_empty_store_has_no_files(self, tmp_path):
        s = StudentMaterialsStore(tmp_path)
        assert s.list_entries() == []

    def test_save_roundtrips_content(self, tmp_path):
        s = StudentMaterialsStore(tmp_path)
        s.save(
            file_id="abc123",
            content=b"hello world",
            title="Greeting",
            filename="greet.md",
        )
        path = s.get_path("abc123")
        assert path.read_bytes() == b"hello world"

    def test_save_persists_metadata(self, tmp_path):
        s = StudentMaterialsStore(tmp_path)
        s.save(
            file_id="abc", content=b"x", title="T", filename="f.md",
        )
        entries = s.list_entries()
        assert len(entries) == 1
        assert entries[0]["file_id"] == "abc"
        assert entries[0]["title"] == "T"

    def test_has_checks_content_file_presence(self, tmp_path):
        s = StudentMaterialsStore(tmp_path)
        assert s.has("abc") is False
        s.save(file_id="abc", content=b"x", title="T", filename="f.md")
        assert s.has("abc") is True

    def test_entries_survive_fresh_instance(self, tmp_path):
        s1 = StudentMaterialsStore(tmp_path)
        s1.save(file_id="abc", content=b"x", title="T", filename="f.md")

        s2 = StudentMaterialsStore(tmp_path)
        assert s2.has("abc") is True
        assert s2.get_path("abc").read_bytes() == b"x"


# ---------------------------------------------------------------------------
# Sync client — happy path
# ---------------------------------------------------------------------------


class TestHappySync:
    def test_fresh_sync_downloads_all_files(
        self, coordinator_with_materials, tmp_path,
    ):
        coord = coordinator_with_materials["coord"]
        materials = coordinator_with_materials["store"]

        manifest = build_materials_manifest(
            identity=coord, classroom_id="NE101", store=materials,
        )

        transport = InProcessGetTransport(
            manifest_json=encode_materials_manifest(manifest),
            file_bytes={
                e.file_id: materials.get_path(e.file_id).read_bytes()
                for e in materials.list_entries()
            },
        )

        student_store = StudentMaterialsStore(tmp_path / "student")
        client = MaterialsSyncClient(
            transport=transport,
            store=student_store,
            coordinator_public_key=coord.public_key,
        )
        result = client.sync(base_url="http://unused/")
        assert result.accepted is True
        assert result.downloaded == 2
        assert result.cached == 0
        assert result.total_bytes > 0

        # Every manifest entry is now on disk.
        for e in manifest.entries:
            assert student_store.has(e.file_id)

    def test_second_sync_is_fully_cached(
        self, coordinator_with_materials, tmp_path,
    ):
        coord = coordinator_with_materials["coord"]
        materials = coordinator_with_materials["store"]

        manifest = build_materials_manifest(
            identity=coord, classroom_id="NE101", store=materials,
        )
        transport = InProcessGetTransport(
            manifest_json=encode_materials_manifest(manifest),
            file_bytes={
                e.file_id: materials.get_path(e.file_id).read_bytes()
                for e in materials.list_entries()
            },
        )

        student_store = StudentMaterialsStore(tmp_path / "student")
        client = MaterialsSyncClient(
            transport=transport,
            store=student_store,
            coordinator_public_key=coord.public_key,
        )
        client.sync(base_url="http://unused/")
        result2 = client.sync(base_url="http://unused/")
        assert result2.downloaded == 0
        assert result2.cached == 2

    def test_incremental_sync_downloads_only_new(
        self, coordinator_with_materials, tmp_path,
    ):
        coord = coordinator_with_materials["coord"]
        materials = coordinator_with_materials["store"]

        # First sync — two entries.
        manifest1 = build_materials_manifest(
            identity=coord, classroom_id="NE101", store=materials,
        )
        transport1 = InProcessGetTransport(
            manifest_json=encode_materials_manifest(manifest1),
            file_bytes={
                e.file_id: materials.get_path(e.file_id).read_bytes()
                for e in materials.list_entries()
            },
        )
        student_store = StudentMaterialsStore(tmp_path / "student")
        MaterialsSyncClient(
            transport=transport1,
            store=student_store,
            coordinator_public_key=coord.public_key,
        ).sync(base_url="http://unused/")

        # Instructor adds a third entry.
        materials.add_text(
            "Chapter 3: Control rods.",
            filename="ch3.md",
        )
        manifest2 = build_materials_manifest(
            identity=coord, classroom_id="NE101", store=materials,
        )
        transport2 = InProcessGetTransport(
            manifest_json=encode_materials_manifest(manifest2),
            file_bytes={
                e.file_id: materials.get_path(e.file_id).read_bytes()
                for e in materials.list_entries()
            },
        )
        result = MaterialsSyncClient(
            transport=transport2,
            store=student_store,
            coordinator_public_key=coord.public_key,
        ).sync(base_url="http://unused/")
        assert result.downloaded == 1
        assert result.cached == 2


# ---------------------------------------------------------------------------
# Signature + tamper defense
# ---------------------------------------------------------------------------


class TestTamperDefense:
    def test_bad_manifest_signature_rejected(
        self, coordinator_with_materials, tmp_path,
    ):
        coord = coordinator_with_materials["coord"]
        materials = coordinator_with_materials["store"]

        # Sign with a DIFFERENT identity — right pubkey won't verify it.
        impostor = generate_identity(
            owner="attacker@x.y", keys_dir=tmp_path / "impostor-keys"
        )
        bogus = build_materials_manifest(
            identity=impostor, classroom_id="NE101", store=materials,
        )
        transport = InProcessGetTransport(
            manifest_json=encode_materials_manifest(bogus),
            file_bytes={},
        )
        student_store = StudentMaterialsStore(tmp_path / "student")
        client = MaterialsSyncClient(
            transport=transport,
            store=student_store,
            coordinator_public_key=coord.public_key,
        )
        with pytest.raises(MaterialsTamperError, match="signature"):
            client.sync(base_url="http://unused/")
        # Nothing got saved.
        assert student_store.list_entries() == []

    def test_content_hash_mismatch_rejected(
        self, coordinator_with_materials, tmp_path,
    ):
        coord = coordinator_with_materials["coord"]
        materials = coordinator_with_materials["store"]

        manifest = build_materials_manifest(
            identity=coord, classroom_id="NE101", store=materials,
        )
        # Substitute garbage bytes for one of the file_ids — the content
        # hash won't match, so the client must refuse.
        good_files = {
            e.file_id: materials.get_path(e.file_id).read_bytes()
            for e in materials.list_entries()
        }
        tampered_id = next(iter(good_files))
        good_files[tampered_id] = b"ATTACKER-SWAPPED-BYTES"
        transport = InProcessGetTransport(
            manifest_json=encode_materials_manifest(manifest),
            file_bytes=good_files,
        )
        student_store = StudentMaterialsStore(tmp_path / "student")
        client = MaterialsSyncClient(
            transport=transport,
            store=student_store,
            coordinator_public_key=coord.public_key,
        )
        with pytest.raises(MaterialsTamperError, match="hash"):
            client.sync(base_url="http://unused/")

    def test_missing_file_surfaces_as_sync_error(
        self, coordinator_with_materials, tmp_path,
    ):
        coord = coordinator_with_materials["coord"]
        materials = coordinator_with_materials["store"]

        manifest = build_materials_manifest(
            identity=coord, classroom_id="NE101", store=materials,
        )
        transport = InProcessGetTransport(
            manifest_json=encode_materials_manifest(manifest),
            file_bytes={},  # server has no files
        )
        student_store = StudentMaterialsStore(tmp_path / "student")
        client = MaterialsSyncClient(
            transport=transport,
            store=student_store,
            coordinator_public_key=coord.public_key,
        )
        result = client.sync(base_url="http://unused/")
        assert result.accepted is False
        assert result.error and "404" in result.error
