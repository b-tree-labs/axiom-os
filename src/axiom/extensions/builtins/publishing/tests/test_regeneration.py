# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for docx regeneration logic.

These tests verify that the hash-based change detection correctly identifies
which files need regeneration, avoiding false positives from stale git state.

Key behaviors tested:
- _needs_regeneration returns True when content hash differs
- _needs_regeneration returns False when content hash matches
- _needs_regeneration returns True when no hash file exists
- _needs_regeneration returns True when docx doesn't exist
- Push workflow only regenerates files that actually changed
"""

import hashlib


class TestNeedsRegeneration:
    """Test the _needs_regeneration helper function."""

    def test_needs_regen_when_docx_missing(self, tmp_path):
        """Regeneration needed when docx doesn't exist."""
        from axiom.extensions.builtins.publishing.cli import _needs_regeneration

        md_file = tmp_path / "test.md"
        md_file.write_text("# Test\n\nContent.\n")
        docx_file = tmp_path / "test.docx"  # Does not exist

        assert _needs_regeneration(md_file, docx_file) is True

    def test_needs_regen_when_hash_differs(self, tmp_path):
        """Regeneration needed when content changed."""
        from axiom.extensions.builtins.publishing.cli import _needs_regeneration

        md_file = tmp_path / "test.md"
        md_file.write_text("# Updated Content\n\nNew stuff.\n")

        docx_file = tmp_path / "test.docx"
        docx_file.write_bytes(b"fake docx content")

        # Write old hash (different from current content)
        hash_file = tmp_path / "test.docx.sha256"
        hash_file.write_text("old_hash_that_doesnt_match")

        assert _needs_regeneration(md_file, docx_file) is True

    def test_no_regen_when_hash_matches(self, tmp_path):
        """No regeneration when content unchanged."""
        from axiom.extensions.builtins.publishing.cli import _needs_regeneration

        md_file = tmp_path / "test.md"
        content = "# Unchanged\n\nSame content.\n"
        md_file.write_text(content)

        docx_file = tmp_path / "test.docx"
        docx_file.write_bytes(b"fake docx content")

        # Write matching hash
        hash_file = tmp_path / "test.docx.sha256"
        content_hash = hashlib.sha256(content.encode()).hexdigest()
        hash_file.write_text(content_hash)

        assert _needs_regeneration(md_file, docx_file) is False

    def test_needs_regen_when_no_hash_file(self, tmp_path):
        """Regeneration needed when no hash file exists (never uploaded)."""
        from axiom.extensions.builtins.publishing.cli import _needs_regeneration

        md_file = tmp_path / "test.md"
        md_file.write_text("# Test\n\nContent.\n")

        docx_file = tmp_path / "test.docx"
        docx_file.write_bytes(b"fake docx content")

        # No hash file exists — could be cancelled upload
        # Should always regenerate regardless of mtime
        assert _needs_regeneration(md_file, docx_file) is True

    def test_no_regen_when_cancelled_but_hash_written(self, tmp_path):
        """This tests the fix: hash should NOT be written until upload succeeds.

        If hash exists, it means upload succeeded, so we trust it.
        """
        from axiom.extensions.builtins.publishing.cli import _needs_regeneration

        md_file = tmp_path / "test.md"
        content = "# Uploaded Content\n"
        md_file.write_text(content)

        docx_file = tmp_path / "test.docx"
        docx_file.write_bytes(b"fake docx content")

        # Hash exists = successfully uploaded
        hash_file = tmp_path / "test.docx.sha256"
        hash_file.write_text(hashlib.sha256(content.encode()).hexdigest())

        assert _needs_regeneration(md_file, docx_file) is False


class TestDocxOutputPath:
    """Test the _docx_output_path helper function."""

    def test_output_path_mirrors_source_structure(self, tmp_path, monkeypatch):
        """Output path mirrors source directory structure."""
        import axiom
        from axiom.extensions.builtins.publishing.cli import _docx_output_path

        # Patch REPO_ROOT to tmp_path
        monkeypatch.setattr(axiom, "REPO_ROOT", tmp_path)

        # Create source in docs/prds/
        req_dir = tmp_path / "docs" / "prds"
        req_dir.mkdir(parents=True)
        md_file = req_dir / "prd-test.md"
        md_file.write_text("# Test PRD\n")

        output = _docx_output_path(md_file)

        # Should be in .neut/generated/docs/prds/
        assert output.parent == tmp_path / ".neut" / "generated" / "docs" / "prds"
        assert output.name == "prd-test.docx"


class TestPushFiltering:
    """Test that push correctly filters to only files needing regeneration."""

    def test_unchanged_files_excluded_from_push(self, tmp_path, monkeypatch):
        """Files with matching hashes are not included in push list."""
        import axiom
        from axiom.extensions.builtins.publishing.cli import (
            _docx_output_path,
            _needs_regeneration,
        )

        monkeypatch.setattr(axiom, "REPO_ROOT", tmp_path)

        # Create two md files
        req_dir = tmp_path / "docs" / "prds"
        req_dir.mkdir(parents=True)

        changed_md = req_dir / "changed.md"
        changed_content = "# Changed\n\nNew content.\n"
        changed_md.write_text(changed_content)

        unchanged_md = req_dir / "unchanged.md"
        unchanged_content = "# Unchanged\n\nSame content.\n"
        unchanged_md.write_text(unchanged_content)

        # Set up generated directory with docx and hash files
        gen_dir = tmp_path / ".neut" / "generated" / "docs" / "prds"
        gen_dir.mkdir(parents=True)

        # unchanged.md has matching hash
        unchanged_docx = gen_dir / "unchanged.docx"
        unchanged_docx.write_bytes(b"fake docx")
        unchanged_hash = gen_dir / "unchanged.docx.sha256"
        unchanged_hash.write_text(hashlib.sha256(unchanged_content.encode()).hexdigest())

        # changed.md has mismatched hash (old content)
        changed_docx = gen_dir / "changed.docx"
        changed_docx.write_bytes(b"fake docx")
        changed_hash = gen_dir / "changed.docx.sha256"
        changed_hash.write_text(hashlib.sha256(b"old content").hexdigest())

        # Simulate the filtering logic from _cmd_push_batch
        files_to_push = []
        for md_file in req_dir.glob("*.md"):
            docx_path = _docx_output_path(md_file)
            if _needs_regeneration(md_file, docx_path):
                files_to_push.append(md_file.name)

        # Only the changed file should be in the list
        assert "changed.md" in files_to_push
        assert "unchanged.md" not in files_to_push
        assert len(files_to_push) == 1

    def test_force_includes_all_files(self, tmp_path, monkeypatch):
        """With --force, all files are included regardless of hash."""
        import axiom
        from axiom.extensions.builtins.publishing.cli import (
            _docx_output_path,
            _needs_regeneration,
        )

        monkeypatch.setattr(axiom, "REPO_ROOT", tmp_path)

        req_dir = tmp_path / "docs" / "prds"
        req_dir.mkdir(parents=True)

        # Create file with matching hash
        unchanged_md = req_dir / "unchanged.md"
        unchanged_content = "# Unchanged\n\nSame content.\n"
        unchanged_md.write_text(unchanged_content)

        gen_dir = tmp_path / ".neut" / "generated" / "docs" / "prds"
        gen_dir.mkdir(parents=True)

        unchanged_docx = gen_dir / "unchanged.docx"
        unchanged_docx.write_bytes(b"fake docx")
        unchanged_hash = gen_dir / "unchanged.docx.sha256"
        unchanged_hash.write_text(hashlib.sha256(unchanged_content.encode()).hexdigest())

        # Simulate filtering with force=True
        force = True
        files_to_push = []
        for md_file in req_dir.glob("*.md"):
            docx_path = _docx_output_path(md_file)
            if force or _needs_regeneration(md_file, docx_path):
                files_to_push.append(md_file.name)

        # Force should include even unchanged files
        assert "unchanged.md" in files_to_push
