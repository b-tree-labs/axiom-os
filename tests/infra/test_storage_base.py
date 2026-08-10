# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for the shared StorageProvider ABC and LocalStorageProvider.

TDD: These tests are written BEFORE the implementation.
"""

from __future__ import annotations

from pathlib import Path

import pytest


class TestStorageProviderABC:
    def test_cannot_instantiate_abc(self):
        from axiom.infra.storage import StorageProvider

        with pytest.raises(TypeError):
            StorageProvider()

    def test_abc_defines_required_methods(self):
        from axiom.infra.storage import StorageProvider

        required = {"upload", "download", "move", "get_canonical_url", "list_artifacts", "delete"}
        abstract = {m for m in dir(StorageProvider) if not m.startswith("_")}
        assert required.issubset(abstract)


class TestLocalStorageProvider:
    @pytest.fixture
    def storage(self, tmp_path):
        from axiom.infra.storage import LocalStorageProvider

        return LocalStorageProvider({"base_dir": str(tmp_path / "store")})

    @pytest.fixture
    def sample_file(self, tmp_path):
        f = tmp_path / "sample.txt"
        f.write_text("hello world")
        return f

    def test_upload_download_roundtrip(self, storage, sample_file, tmp_path):
        result = storage.upload(sample_file, "test/sample.txt")
        assert result.storage_id == "test/sample.txt"
        assert result.success is True

        dest = tmp_path / "downloaded.txt"
        storage.download("test/sample.txt", dest)
        assert dest.read_text() == "hello world"

    def test_list_artifacts(self, storage, sample_file):
        storage.upload(sample_file, "a.txt")
        storage.upload(sample_file, "b.txt")
        storage.upload(sample_file, "sub/c.txt")

        entries = storage.list_artifacts()
        names = {e.name for e in entries}
        assert names == {"a.txt", "b.txt", "c.txt"}
        assert len(entries) == 3

    def test_delete(self, storage, sample_file):
        storage.upload(sample_file, "to-delete.txt")
        assert storage.delete("to-delete.txt") is True
        assert storage.delete("to-delete.txt") is False  # already gone

    def test_move(self, storage, sample_file):
        storage.upload(sample_file, "old/file.txt")
        result = storage.move("old/file.txt", "new/file.txt")
        assert result.storage_id == "new/file.txt"

        # Old path gone
        with pytest.raises(FileNotFoundError):
            storage.download("old/file.txt", Path("/dev/null"))

    def test_download_nonexistent_raises(self, storage, tmp_path):
        with pytest.raises(FileNotFoundError):
            storage.download("nonexistent.txt", tmp_path / "out.txt")

    def test_upload_with_metadata(self, storage, sample_file):
        result = storage.upload(sample_file, "meta.txt", metadata={"version": "v2"})
        assert result.version == "v2"
