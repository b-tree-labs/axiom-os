# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for the instructor-side classroom materials store.

Phase 1 of the materials-flow tier: when the instructor uploads files
via `axi classroom prep corpus`, they must survive to disk so the
coordinator can later serve them to joining students. Today uploads
are only in-memory — this module fixes that.

Contract:
- Files are content-addressed (sha256); identical uploads dedupe.
- A signed manifest can be rebuilt from disk state at any time (Phase 2).
- Deleting a classroom's materials dir is the "forget this class"
  button; no hidden state elsewhere.
"""

from __future__ import annotations

import json

import pytest

from axiom.extensions.builtins.classroom.classroom_materials import (
    ClassroomMaterialsStore,
    compute_file_id,
)

# ---------------------------------------------------------------------------
# Content addressing
# ---------------------------------------------------------------------------


class TestContentAddressing:
    def test_file_id_is_deterministic(self):
        assert compute_file_id(b"hello world") == compute_file_id(b"hello world")

    def test_file_id_differs_for_different_content(self):
        assert compute_file_id(b"hello") != compute_file_id(b"world")

    def test_file_id_is_url_safe(self):
        fid = compute_file_id(b"arbitrary bytes \x00\x01\x02")
        assert fid.isalnum() or all(c.isalnum() or c == "-" or c == "_" for c in fid)


# ---------------------------------------------------------------------------
# Basic add + list
# ---------------------------------------------------------------------------


class TestAddAndList:
    def test_empty_store_lists_nothing(self, tmp_path):
        store = ClassroomMaterialsStore(tmp_path)
        assert store.list_entries() == []

    def test_add_text_persists(self, tmp_path):
        store = ClassroomMaterialsStore(tmp_path)
        entry = store.add_text(
            "Fission splits heavy nuclei.",
            filename="chapter1.md",
        )
        assert entry.filename == "chapter1.md"
        assert entry.title == "chapter1.md"  # default = filename
        assert entry.size_bytes == len(b"Fission splits heavy nuclei.")
        assert entry.added_at  # ISO timestamp present

        # Visible in list.
        listed = store.list_entries()
        assert len(listed) == 1
        assert listed[0].file_id == entry.file_id

    def test_add_file_persists_and_reads_back(self, tmp_path):
        src = tmp_path / "source.md"
        src.write_text("# Nuclear 101\n\nContent here.")
        store = ClassroomMaterialsStore(tmp_path / "store")

        entry = store.add_file(src)
        assert entry.filename == "source.md"

        # Content readable back from the store.
        path = store.get_path(entry.file_id)
        assert path.read_text() == "# Nuclear 101\n\nContent here."

    def test_explicit_title_overrides_filename(self, tmp_path):
        store = ClassroomMaterialsStore(tmp_path)
        entry = store.add_text(
            "content",
            filename="raw.md",
            title="Chapter 1 — Fission",
        )
        assert entry.title == "Chapter 1 — Fission"
        assert entry.filename == "raw.md"


# ---------------------------------------------------------------------------
# Content-addressed dedup
# ---------------------------------------------------------------------------


class TestDedup:
    def test_identical_text_reuses_same_file_id(self, tmp_path):
        store = ClassroomMaterialsStore(tmp_path)
        a = store.add_text("duplicate content", filename="a.md")
        b = store.add_text("duplicate content", filename="b.md")
        # Same bytes → same file_id — content is physically stored once.
        assert a.file_id == b.file_id
        # But the second upload updates metadata (filename / title), so
        # there's still only one entry per file_id. Two uploads of the same
        # bytes result in one listing.
        entries = store.list_entries()
        assert len(entries) == 1
        # The later upload's filename wins (last writer).
        assert entries[0].filename == "b.md"

    def test_different_filenames_same_bytes_one_entry(self, tmp_path):
        store = ClassroomMaterialsStore(tmp_path)
        store.add_text("payload", filename="one.md")
        store.add_text("payload", filename="two.md")
        assert len(store.list_entries()) == 1


# ---------------------------------------------------------------------------
# Durability
# ---------------------------------------------------------------------------


class TestDurability:
    def test_entries_survive_fresh_instance(self, tmp_path):
        s1 = ClassroomMaterialsStore(tmp_path)
        e1 = s1.add_text("content one", filename="one.md")
        e2 = s1.add_text("content two", filename="two.md")

        s2 = ClassroomMaterialsStore(tmp_path)
        ids = {e.file_id for e in s2.list_entries()}
        assert ids == {e1.file_id, e2.file_id}

    def test_index_file_is_readable_json(self, tmp_path):
        store = ClassroomMaterialsStore(tmp_path)
        store.add_text("hello", filename="a.md")

        # The metadata index lives at a fixed path; instructors should
        # be able to eyeball it for debugging.
        index_path = tmp_path / "materials_index.json"
        assert index_path.is_file()
        data = json.loads(index_path.read_text())
        assert "entries" in data
        assert len(data["entries"]) == 1


# ---------------------------------------------------------------------------
# Removal
# ---------------------------------------------------------------------------


class TestRemove:
    def test_remove_known_entry(self, tmp_path):
        store = ClassroomMaterialsStore(tmp_path)
        entry = store.add_text("to be removed", filename="x.md")
        content_path = tmp_path / "materials" / entry.file_id
        assert content_path.is_file()  # pre-condition

        store.remove(entry.file_id)
        assert store.list_entries() == []
        # Content file is gone too (get_path would now raise KeyError).
        assert not content_path.exists()

    def test_remove_unknown_entry_is_noop(self, tmp_path):
        store = ClassroomMaterialsStore(tmp_path)
        store.remove("never-seen-id")  # does not raise
        assert store.list_entries() == []


# ---------------------------------------------------------------------------
# Missing file handling
# ---------------------------------------------------------------------------


class TestMissingFiles:
    def test_get_path_unknown_raises(self, tmp_path):
        store = ClassroomMaterialsStore(tmp_path)
        with pytest.raises(KeyError):
            store.get_path("does-not-exist")

    def test_add_file_missing_source_raises(self, tmp_path):
        store = ClassroomMaterialsStore(tmp_path)
        with pytest.raises(FileNotFoundError):
            store.add_file(tmp_path / "nope.md")


# ---------------------------------------------------------------------------
# Disk layout — locked so Phase 2 serving code can depend on it
# ---------------------------------------------------------------------------


class TestDiskLayout:
    def test_content_stored_under_file_id_path(self, tmp_path):
        store = ClassroomMaterialsStore(tmp_path)
        entry = store.add_text("exact bytes", filename="x.md")
        content_path = tmp_path / "materials" / entry.file_id
        assert content_path.is_file()
        assert content_path.read_bytes() == b"exact bytes"
