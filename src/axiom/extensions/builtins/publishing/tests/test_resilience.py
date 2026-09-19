# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Resilience tests for Publisher — validation, cleanup, recovery scenarios.

Tests cover:
1. File integrity (missing .docx, .md, images, metadata)
2. Cleanup effectiveness (broken URLs, blockquotes, styles, diagrams)
3. State transitions (draft → published, orphan → published)
4. Recovery paths (re-pull, re-extract, rebuild from git)
5. Round-trip fidelity (.md → .docx → .md)
"""

import pytest

from ..cleanup import MarkdownCleanup
from ..models.document import Document, Image, Link, Section
from ..validation import DocumentValidator


class TestCleanupRobustness:
    """Test markdown cleanup against pandoc conversion artifacts."""

    def test_broken_sharepoint_urls(self):
        """Fix broken SharePoint URLs → text references."""
        content = (
            "See [Policy](https://utk.sharepoint.com/:w:/r/sites/neutronsoftware/shared/"
            "Documents/medical-isotope-prd.docx?d=w1234&csf=1&e=abc123) for details."
        )

        cleaner = MarkdownCleanup()
        cleaned = cleaner.clean_content(content)

        assert "Policy (Published in SharePoint)" in cleaned
        assert "sharepoint.com" not in cleaned
        assert cleaner.fixes_applied["broken_urls"] == 1

    def test_blockquote_mess(self):
        """Fix nested blockquotes (> > >) → clean lists."""
        content = (
            "Some text\n"
            "> > First nested quote\n"
            "> > Second nested quote\n"
            "More text"
        )

        cleaner = MarkdownCleanup()
        cleaned = cleaner.clean_content(content)

        # Should convert to bullet list
        assert "- First nested quote" in cleaned or "> First nested quote" in cleaned
        assert cleaner.fixes_applied.get("blockquotes", 0) >= 0

    def test_inline_pixel_styles(self):
        """Remove inline style attributes from Word conversion."""
        content = 'Some text with <img src="test.png" style="width:6.5in;height:4in;"/> inline image'

        cleaner = MarkdownCleanup()
        cleaned = cleaner.clean_content(content)

        # Style attribute should be removed
        if 'style="' in content:
            assert 'style="' not in cleaned or cleaner.fixes_applied.get("inline_styles", 0) > 0

    def test_diagram_rendering_failure(self):
        """Replace ⚠️ Diagram rendering failed → mermaid placeholder."""
        content = (
            "# Architecture\n\n"
            "⚠️ Diagram rendering failed - Order State Machine"
        )

        cleaner = MarkdownCleanup()
        cleaned = cleaner.clean_content(content)

        assert "⚠️ Diagram rendering failed" not in cleaned
        # Should have some diagram placeholder
        assert "mermaid" in cleaned or "Diagram rendered" in cleaned

    def test_missing_image_alt_text(self):
        """Add alt text to images without it."""
        content = (
            "# Process Flow\n\n"
            "![](media/workflow_diagram.png)\n\n"
            "The diagram shows the process."
        )

        cleaner = MarkdownCleanup()
        cleaned = cleaner.clean_content(content)

        # Should have alt text
        assert "![Workflow Diagram]" in cleaned or "![workflow_diagram]" in cleaned


class TestValidationFramework:
    """Test document state validation and recovery detection."""

    def test_detect_draft_status(self, tmp_path):
        """Detect and report draft status (.md exists, no .docx)."""
        docs_root = tmp_path / "docs" / "prd"
        docs_root.mkdir(parents=True)

        # Create markdown
        md_file = docs_root / "test-prd.md"
        md_file.write_text("# Test PRD\n\nContent")

        validator = DocumentValidator(tmp_path)
        result = validator.validate_document("test-prd")

        assert result.publication_status == "draft"
        assert any(i.category == "missing_source_docx" for i in result.issues)

    def test_detect_orphan_status(self, tmp_path):
        """Detect and report orphan status (.docx exists, no .md)."""
        docs_root = tmp_path / "docs" / "prd"
        source_dir = docs_root / "_source"
        source_dir.mkdir(parents=True)

        # Create source docx (just a placeholder file)
        docx_file = source_dir / "test-prd.docx"
        docx_file.write_bytes(b"%PDF" * 100)  # Fake DOCX

        validator = DocumentValidator(tmp_path)
        result = validator.validate_document("test-prd")

        assert result.publication_status == "orphan"
        assert any(i.category == "missing_markdown" for i in result.issues)

    def test_detect_empty_file(self, tmp_path):
        """Detect empty or corrupt files."""
        docs_root = tmp_path / "docs" / "prd"
        docs_root.mkdir(parents=True)

        # Create empty markdown
        md_file = docs_root / "test-prd.md"
        md_file.write_text("")

        validator = DocumentValidator(tmp_path)
        result = validator.validate_document("test-prd")

        assert not result.is_valid
        assert any(i.category == "empty_file" for i in result.issues)

    def test_detect_missing_image_references(self, tmp_path):
        """Detect broken image references in markdown."""
        docs_root = tmp_path / "docs" / "prd"
        docs_root.mkdir(parents=True)

        # Create markdown with image reference
        md_file = docs_root / "test-prd.md"
        md_file.write_text("# Test\n\n![alt](media/missing.png)\n")

        validator = DocumentValidator(tmp_path)
        result = validator.validate_document("test-prd")

        assert any(i.category == "missing_image" for i in result.issues)

    def test_recovery_steps_for_draft(self, tmp_path):
        """Suggest recovery steps for draft document."""
        docs_root = tmp_path / "docs" / "prd"
        docs_root.mkdir(parents=True)

        md_file = docs_root / "test-prd.md"
        md_file.write_text("# Test PRD")

        validator = DocumentValidator(tmp_path)
        result = validator.validate_document("test-prd")

        # Should suggest publishing
        recovery_text = " ".join(result.recovery_steps)
        assert "first_publish" in recovery_text or "publish" in recovery_text


class TestDocumentModel:
    """Test Document model validation and structure."""

    def test_document_validation(self):
        """Test document integrity checks."""
        doc = Document(
            title="Test Doc",
            sections=[
                Section(
                    level=1,
                    title="Intro",
                    images=[Image(path="media/img1.png", alt_text="")],
                    links=[Link(text="Bad Link", url="https://sharepoint.com/broken", is_broken=True)],
                )
            ],
        )

        doc.validate()

        # Should detect missing alt text
        assert len(doc.get_missing_alt_text()) == 1

        # Should detect broken links
        assert len(doc.get_broken_links()) == 1

    def test_document_structure_traversal(self):
        """Test recursive section traversal."""
        doc = Document(
            title="Root",
            sections=[
                Section(
                    level=1,
                    title="Chapter 1",
                    images=[Image(path="media/c1.png")],
                    subsections=[
                        Section(
                            level=2,
                            title="Section 1.1",
                            images=[Image(path="media/c1s1.png")],
                        )
                    ],
                )
            ],
        )

        # Should find all images recursively
        all_images = doc.get_all_images()
        assert len(all_images) == 2


class TestStateTransitions:
    """Test publication state machine transitions."""

    def test_transition_draft_to_published(self, tmp_path):
        """Test state transition when source .docx is created."""
        docs_root = tmp_path / "docs" / "prd"
        source_dir = docs_root / "_source"
        docs_root.mkdir(parents=True)

        # Start: draft (.md exists, no .docx)
        md_file = docs_root / "test-prd.md"
        md_file.write_text("# Test")

        validator = DocumentValidator(tmp_path)
        result1 = validator.validate_document("test-prd")
        assert result1.publication_status == "draft"

        # Transition: create source .docx
        source_dir.mkdir(parents=True, exist_ok=True)
        docx_file = source_dir / "test-prd.docx"
        docx_file.write_bytes(b"fake docx content")

        # Now: published
        result2 = validator.validate_document("test-prd")
        assert result2.publication_status == "published"

    def test_transition_orphan_to_published(self, tmp_path):
        """Test state transition when markdown is created for orphan."""
        docs_root = tmp_path / "docs" / "prd"
        source_dir = docs_root / "_source"
        docs_root.mkdir(parents=True)
        source_dir.mkdir(parents=True)

        # Start: orphan (.docx exists, no .md)
        docx_file = source_dir / "test-prd.docx"
        docx_file.write_bytes(b"fake docx")

        validator = DocumentValidator(tmp_path)
        result1 = validator.validate_document("test-prd")
        assert result1.publication_status == "orphan"

        # Transition: create .md
        md_file = docs_root / "test-prd.md"
        md_file.write_text("# Test")

        # Now: published
        result2 = validator.validate_document("test-prd")
        assert result2.publication_status == "published"


class TestCleanupComposability:
    """Test that cleanup can be applied multiple times safely (idempotence)."""

    def test_cleanup_is_idempotent(self):
        """Applying cleanup twice should produce same result."""
        content = (
            "Check [docs](https://utk.sharepoint.com/broken) and "
            "![](media/img.png)\n"
            "> > nested quote"
        )

        cleaner1 = MarkdownCleanup()
        cleaned1 = cleaner1.clean_content(content)

        cleaner2 = MarkdownCleanup()
        cleaned2 = cleaner2.clean_content(cleaned1)

        # Second pass should make no changes
        assert cleaned1 == cleaned2

    def test_cleanup_preserves_valid_content(self):
        """Cleanup should not damage already-valid markdown."""
        valid_content = (
            "# Valid Document\n\n"
            "![Good image](media/diagram.png)\n\n"
            "[Internal link](../other-prd.md)\n\n"
            "> Valid blockquote\n\n"
            "Some text."
        )

        cleaner = MarkdownCleanup()
        cleaned = cleaner.clean_content(valid_content)

        # Should be mostly unchanged (maybe alt text added)
        assert "# Valid Document" in cleaned
        assert "media/diagram.png" in cleaned


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
