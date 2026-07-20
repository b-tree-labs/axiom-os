# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for document state persistence."""

import pytest

from axiom.extensions.builtins.publishing.state import (
    DocumentState,
    PublicationRecord,
    StateStore,
)


@pytest.fixture
def store(tmp_path):
    return StateStore(tmp_path / "publisher-state.json")


class TestStateStore:
    """Tests for state store persistence and querying."""

    def test_empty_store(self, store):
        assert store.count == 0
        assert store.list_by_status() == []

    def test_add_document(self, store):
        doc = DocumentState(doc_id="test", source_path="docs/test.md")
        store.update(doc)
        assert store.count == 1

    def test_get_by_id(self, store):
        store.update(DocumentState(doc_id="alpha", source_path="docs/alpha.md"))
        result = store.get("alpha")
        assert result is not None
        assert result.source_path == "docs/alpha.md"

    def test_get_by_path(self, store):
        store.update(DocumentState(doc_id="beta", source_path="docs/prds/prd_beta.md"))
        result = store.get_by_path("docs/prds/prd_beta.md")
        assert result is not None
        assert result.doc_id == "beta"

    def test_get_nonexistent(self, store):
        assert store.get("nonexistent") is None
        assert store.get_by_path("nonexistent.md") is None

    def test_persistence(self, tmp_path):
        path = tmp_path / "publisher-state.json"

        s1 = StateStore(path)
        s1.update(DocumentState(doc_id="doc1", source_path="docs/doc1.md", status="published"))

        s2 = StateStore(path)
        assert s2.count == 1
        doc1 = s2.get("doc1")
        assert doc1 is not None
        assert doc1.status == "published"

    def test_list_by_status(self, store):
        store.update(DocumentState(doc_id="a", source_path="a.md", status="local"))
        store.update(DocumentState(doc_id="b", source_path="b.md", status="published"))
        store.update(DocumentState(doc_id="c", source_path="c.md", status="published"))

        published = store.list_by_status("published")
        assert len(published) == 2

        local = store.list_by_status("local")
        assert len(local) == 1

        all_docs = store.list_by_status()
        assert len(all_docs) == 3

    def test_remove(self, store):
        store.update(DocumentState(doc_id="remove-me", source_path="r.md"))
        assert store.remove("remove-me") is True
        assert store.count == 0

    def test_remove_nonexistent(self, store):
        assert store.remove("ghost") is False

    def test_update_overwrites(self, store):
        store.update(DocumentState(doc_id="doc", source_path="doc.md", status="local"))
        store.update(DocumentState(doc_id="doc", source_path="doc.md", status="published"))
        assert store.count == 1
        doc = store.get("doc")
        assert doc is not None
        assert doc.status == "published"

    def test_complex_state_persistence(self, tmp_path):
        """State with publication records persists correctly."""
        path = tmp_path / "publisher-state.json"
        store = StateStore(path)

        pub = PublicationRecord(
            storage_id="id1",
            url="https://example.com/doc.docx",
            version="v2",
            published_at="2026-02-15T10:00:00Z",
            commit_sha="abc123",
            generation_provider="pandoc-docx",
            storage_provider="local",
        )
        doc = DocumentState(
            doc_id="complex",
            source_path="docs/complex.md",
            status="published",
            published=pub,
            last_commit="abc123",
            last_branch="main",
        )
        store.update(doc)

        # Reload
        store2 = StateStore(path)
        restored = store2.get("complex")
        assert restored is not None
        assert restored.published is not None
        assert restored.published.version == "v2"
        assert restored.published.storage_provider == "local"
