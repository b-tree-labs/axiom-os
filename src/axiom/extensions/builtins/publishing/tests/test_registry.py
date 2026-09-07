# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the link registry."""

import pytest

from axiom.extensions.builtins.publishing.registry import LinkRegistry
from axiom.extensions.builtins.publishing.state import LinkEntry


@pytest.fixture
def registry(tmp_path):
    return LinkRegistry(tmp_path / "publisher-registry.json")


class TestLinkRegistry:
    """Tests for link registry persistence and querying."""

    def test_empty_registry(self, registry):
        assert registry.count == 0
        assert registry.build_link_map() == {}

    def test_add_entry(self, registry):
        entry = LinkEntry(
            doc_id="test-doc",
            source_path="docs/prds/prd_test-doc.md",
            published_url="file:///output/test-doc.docx",
            storage_id="local/test-doc.docx",
            version="v1",
        )
        registry.update(entry)
        assert registry.count == 1

    def test_persistence(self, tmp_path):
        """Registry persists to disk and can be reloaded."""
        path = tmp_path / "publisher-registry.json"

        reg1 = LinkRegistry(path)
        reg1.update(LinkEntry(
            doc_id="alpha",
            source_path="docs/prds/prd_alpha.md",
            published_url="https://example.com/alpha.docx",
        ))

        # Create new instance from same file
        reg2 = LinkRegistry(path)
        assert reg2.count == 1
        assert reg2.get("docs/prds/prd_alpha.md") is not None

    def test_get_by_source_path(self, registry):
        registry.update(LinkEntry(
            doc_id="spec",
            source_path="docs/specs/spec.md",
            published_url="file:///spec.docx",
        ))
        result = registry.get("docs/specs/spec.md")
        assert result is not None
        assert result.doc_id == "spec"

    def test_get_by_doc_id(self, registry):
        registry.update(LinkEntry(
            doc_id="my-doc",
            source_path="docs/prds/prd_my-doc.md",
            published_url="file:///my-doc.docx",
        ))
        result = registry.get_by_doc_id("my-doc")
        assert result is not None
        assert result.source_path == "docs/prds/prd_my-doc.md"

    def test_get_nonexistent(self, registry):
        assert registry.get("nonexistent.md") is None
        assert registry.get_by_doc_id("nonexistent") is None

    def test_remove(self, registry):
        registry.update(LinkEntry(
            doc_id="removeme",
            source_path="docs/removeme.md",
            published_url="file:///removeme.docx",
        ))
        assert registry.count == 1
        assert registry.remove("docs/removeme.md") is True
        assert registry.count == 0

    def test_remove_nonexistent(self, registry):
        assert registry.remove("nonexistent.md") is False

    def test_build_link_map(self, registry):
        """Link map provides multiple key formats for matching."""
        registry.update(LinkEntry(
            doc_id="experiment-prd",
            source_path="docs/prds/prd_experiment.md",
            published_url="https://sharepoint.com/experiment-prd.docx",
        ))

        link_map = registry.build_link_map()

        # Should have full path, filename, and stem+.md entries
        assert "docs/prds/prd_experiment.md" in link_map
        assert "prd_experiment.md" in link_map
        assert link_map["docs/prds/prd_experiment.md"] == "https://sharepoint.com/experiment-prd.docx"

    def test_link_map_excludes_empty_urls(self, registry):
        """Entries without published_url are excluded from link map."""
        registry.update(LinkEntry(
            doc_id="draft-only",
            source_path="docs/draft.md",
            published_url="",
            draft_url="file:///draft.docx",
        ))
        link_map = registry.build_link_map()
        assert "docs/draft.md" not in link_map

    def test_update_overwrites(self, registry):
        """Updating an entry with same source_path overwrites."""
        registry.update(LinkEntry(
            doc_id="doc",
            source_path="docs/doc.md",
            published_url="https://v1.com",
            version="v1",
        ))
        registry.update(LinkEntry(
            doc_id="doc",
            source_path="docs/doc.md",
            published_url="https://v2.com",
            version="v2",
        ))
        assert registry.count == 1
        assert registry.get("docs/doc.md").version == "v2"

    def test_check_links(self, registry, tmp_path):
        """Check links identifies valid and missing source files."""
        # Create a fake repo root with one existing doc
        fake_root = tmp_path / "fakerepo"
        (fake_root / "docs" / "specs").mkdir(parents=True)
        (fake_root / "docs" / "specs" / "docflow-spec.md").write_text("# Spec")

        # Register a document that exists
        registry.update(LinkEntry(
            doc_id="docflow-spec",
            source_path="docs/specs/docflow-spec.md",
            published_url="file:///docflow-spec.docx",
        ))
        # Register a document that doesn't exist
        registry.update(LinkEntry(
            doc_id="ghost",
            source_path="docs/ghost.md",
            published_url="file:///ghost.docx",
        ))

        results = registry.check_links(fake_root / "docs")
        assert "docs/specs/docflow-spec.md" in results["valid"]
        assert "docs/ghost.md" in results["missing"]
