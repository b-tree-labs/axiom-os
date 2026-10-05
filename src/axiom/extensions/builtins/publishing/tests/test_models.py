# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for docflow data models."""

from axiom.extensions.builtins.publishing.state import (
    Comment,
    DocumentState,
    LinkEntry,
    PublicationRecord,
)


class TestComment:
    """Tests for the Comment model."""

    def test_basic_construction(self):
        c = Comment(
            comment_id="1",
            author="Alice",
            timestamp="2026-02-15T10:00:00Z",
            text="Looks good!",
        )
        assert c.comment_id == "1"
        assert c.resolved is False
        assert c.replies == []

    def test_to_dict_roundtrip(self):
        original = Comment(
            comment_id="42",
            author="Bob",
            timestamp="2026-02-15T10:00:00Z",
            text="Fix typo on page 3",
            context="The system uses...",
            resolved=True,
            source="docx-comments",
            replies=[
                Comment(
                    comment_id="43",
                    author="Alice",
                    timestamp="2026-02-15T11:00:00Z",
                    text="Fixed!",
                )
            ],
        )
        d = original.to_dict()
        restored = Comment.from_dict(d)

        assert restored.comment_id == "42"
        assert restored.resolved is True
        assert len(restored.replies) == 1
        assert restored.replies[0].text == "Fixed!"


class TestLinkEntry:
    """Tests for the LinkEntry model."""

    def test_construction(self):
        entry = LinkEntry(
            doc_id="test-doc",
            source_path="docs/prds/prd_test-doc.md",
            published_url="https://example.com/test-doc.docx",
        )
        assert entry.version == "v1"
        assert entry.draft_url is None

    def test_roundtrip(self):
        original = LinkEntry(
            doc_id="spec",
            source_path="docs/specs/spec.md",
            published_url="file:///output/spec.docx",
            storage_id="local/spec.docx",
            last_published="2026-02-15T10:00:00Z",
            version="v3",
            commit_sha="abc123",
        )
        d = original.to_dict()
        restored = LinkEntry.from_dict(d)

        assert restored.doc_id == "spec"
        assert restored.version == "v3"
        assert restored.commit_sha == "abc123"


class TestPublicationRecord:
    """Tests for the PublicationRecord model."""

    def test_roundtrip(self):
        original = PublicationRecord(
            storage_id="file123",
            url="https://example.com/doc.docx",
            version="v2",
            published_at="2026-02-15T10:00:00Z",
            commit_sha="def456",
            generation_provider="pandoc-docx",
            storage_provider="local",
        )
        d = original.to_dict()
        restored = PublicationRecord.from_dict(d)

        assert restored.storage_id == "file123"
        assert restored.generation_provider == "pandoc-docx"
        assert restored.storage_provider == "local"


class TestDocumentState:
    """Tests for the DocumentState model."""

    def test_default_state(self):
        doc = DocumentState(doc_id="test", source_path="docs/test.md")
        assert doc.status == "draft"
        assert doc.published is None
        assert doc.pending_comments == []
        assert doc.stakeholders == []

    def test_full_roundtrip(self):
        pub = PublicationRecord(
            storage_id="id1",
            url="https://example.com/test.docx",
            version="v1",
            published_at="2026-02-15T10:00:00Z",
            commit_sha="abc",
            generation_provider="pandoc-docx",
            storage_provider="onedrive",
        )
        comment = Comment(
            comment_id="c1",
            author="Reviewer",
            timestamp="2026-02-16T10:00:00Z",
            text="Add more detail",
        )
        original = DocumentState(
            doc_id="test-doc",
            source_path="docs/prds/prd_test-doc.md",
            status="published",
            published=pub,
            last_commit="abc",
            last_branch="main",
            pending_comments=[comment.to_dict()],
            stakeholders=["alice", "bob"],
        )

        d = original.to_dict()
        restored = DocumentState.from_dict(d)

        assert restored.status == "published"
        assert restored.published is not None
        assert restored.published.url == "https://example.com/test.docx"
        assert len(restored.pending_comments) == 1
        assert restored.stakeholders == ["alice", "bob"]
