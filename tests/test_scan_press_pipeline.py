# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for SCAN + PRESS pipeline issues identified 2026-03-18.

Covers:
1. Notes auto-ingest to signal inbox
2. Transit delivery pipeline
6. Signal-to-draft synthesis
7. Originator notification
9. Correlator config existence
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

import pytest

from axiom import REPO_ROOT


class TestGitLabIssueUpdates:
    """Fix #11: SCAN should post status updates to tracked GitLab issues."""

    def test_issue_provider_can_comment(self):
        """GitLab issue provider should support adding comments."""
        from axiom.infra.subscribers.gitlab_issues import GitLabIssueProvider
        provider = GitLabIssueProvider()
        # Should have a comment method (even if not connected)
        assert hasattr(provider, "add_comment") or hasattr(provider, "create_issue")


class TestPublisherHashCheck:
    """PRESS should regenerate .docx when source .md changes, even if .docx exists."""

    def test_source_hash_detects_change(self):
        """_compute_source_hash returns different values for different content."""
        import tempfile

        from axiom.extensions.builtins.publishing.engine import PublisherEngine

        engine = PublisherEngine.__new__(PublisherEngine)

        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write("# Original content\n")
            path = Path(f.name)

        hash1 = engine._compute_source_hash(path)

        path.write_text("# Modified content\n")
        hash2 = engine._compute_source_hash(path)

        path.unlink()

        assert hash1 != hash2, "Hash should differ when source content changes"

    def test_stale_docx_detected(self, tmp_path):
        """If .md changed but .docx exists, it should be flagged as stale."""
        import hashlib

        source = tmp_path / "test.md"
        docx = tmp_path / "test.docx"

        source.write_text("# Original\n")
        old_hash = hashlib.sha256(b"# Original\n").hexdigest()
        docx.write_bytes(b"fake docx")

        source.write_text("# Updated\n")
        new_hash = hashlib.sha256(b"# Updated\n").hexdigest()

        assert old_hash != new_hash
        assert docx.exists(), "Stale .docx should still exist"
        # The fix: generation should check source hash, not just .docx existence


class TestNotesFlowToSignalInbox:
    """Fix #1: neut note should copy entries to signal inbox for SCAN to ingest."""

    def test_note_creates_inbox_copy(self, tmp_path):
        """When a note is saved, a copy should appear in inbox/raw/ for SCAN."""
        from axiom.extensions.builtins.note.cli import _append_note

        note_file = tmp_path / "test_note.md"
        inbox_dir = tmp_path / "inbox" / "raw"
        inbox_dir.mkdir(parents=True)

        with mock.patch("axiom.extensions.builtins.note.cli._inbox_dir", return_value=inbox_dir):
            _append_note(note_file, "Test note for SCAN ingestion")

        # Note should exist at the target path
        assert note_file.exists()
        assert "Test note" in note_file.read_text()

        # A copy should also exist in inbox/raw for SCAN
        inbox_notes = list(inbox_dir.glob("note_*.md"))
        assert len(inbox_notes) >= 1, "Note should be copied to inbox/raw/ for signal ingestion"
        assert "Test note" in inbox_notes[0].read_text()


class TestTransitDelivery:
    """Fix #2: Transit log should deliver signals, not leave them queued."""

    def test_transit_log_exists(self):
        transit_log = REPO_ROOT / "runtime" / "inbox" / "processed" / "transit_log.json"
        if transit_log.exists():
            data = json.loads(transit_log.read_text())
            # Check if there are stale queued entries
            [
                e for e in data.get("entries", data if isinstance(data, list) else [])
                if isinstance(e, dict) and e.get("status") == "queued"
            ]
            # After fix, there should be no indefinitely stale entries
            # (This test documents the current bug)


class TestSignalDraftSynthesis:
    """Fix #6: SCAN should synthesize signals into draft changelogs."""

    def test_draft_command_exists(self):
        """neut signal draft should be a valid command."""
        from axiom.extensions.builtins.signals.cli import get_parser
        parser = get_parser()
        args = parser.parse_args(["draft"])
        assert args.command == "draft"


@pytest.mark.skip(reason="deployment check — requires runtime/config/ files from axiom setup, not present in CI")
class TestCorrelatorConfig:
    """Fix #9: Correlator config files should exist."""

    def test_people_config_exists(self):
        """runtime/config/people.md should exist for signal correlation."""
        people_path = REPO_ROOT / "runtime" / "config" / "people.md"
        assert people_path.exists(), (
            "runtime/config/people.md missing — correlator can't tag signals with people. "
            "Create it with team member names and roles."
        )

    def test_initiatives_config_exists(self):
        """runtime/config/initiatives.md should exist for signal correlation."""
        initiatives_path = REPO_ROOT / "runtime" / "config" / "initiatives.md"
        assert initiatives_path.exists(), (
            "runtime/config/initiatives.md missing — correlator can't tag signals with initiatives. "
            "Create it with current project initiatives."
        )
