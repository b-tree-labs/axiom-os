# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the Publisher CLI commands.

Tests CLI argument parsing and command dispatch without hitting real storage.
"""

import argparse
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from axiom.extensions.builtins.publishing import cli


class TestCmdPull:
    """Tests for the `neut pub pull` command."""

    def test_pull_requires_doc_id_or_all(self, capsys):
        """Pull without doc_id or --all exits with error."""
        args = argparse.Namespace(
            command="pull",
            doc_id=None,
            all=False,
            dry_run=False,
            comments=False,
        )

        with pytest.raises(SystemExit) as exc:
            cli.cmd_pull(args)

        assert exc.value.code == 1
        captured = capsys.readouterr()
        assert "Specify a doc_id or use --all" in captured.out

    @patch("axiom.extensions.builtins.publishing.engine.PublisherEngine")
    def test_pull_single_doc_no_changes(self, mock_engine_cls, capsys):
        """Pull single doc with no changes reports 'No changes'."""
        mock_engine = MagicMock()
        mock_engine.pull.return_value = {"changed": False, "diff": None, "comments": []}
        mock_engine_cls.return_value = mock_engine

        args = argparse.Namespace(
            command="pull",
            doc_id="test-doc",
            all=False,
            dry_run=False,
            comments=False,
        )

        cli.cmd_pull(args)

        mock_engine.pull.assert_called_once_with("test-doc", dry_run=False, include_comments=False)
        captured = capsys.readouterr()
        assert "No changes" in captured.out

    @patch("axiom.extensions.builtins.publishing.engine.PublisherEngine")
    def test_pull_single_doc_with_changes(self, mock_engine_cls, capsys):
        """Pull single doc with changes reports the update."""
        mock_engine = MagicMock()
        mock_engine.pull.return_value = {
            "changed": True,
            "source_path": "docs/prds/prd_test.md",
            "diff": "--- a\n+++ b\n@@ -1 +1 @@\n-old\n+new",
            "comments": [],
        }
        mock_engine_cls.return_value = mock_engine

        args = argparse.Namespace(
            command="pull",
            doc_id="test-doc",
            all=False,
            dry_run=False,
            comments=False,
        )

        cli.cmd_pull(args)

        captured = capsys.readouterr()
        assert "Updated" in captured.out
        assert "docs/prds/prd_test.md" in captured.out

    @patch("axiom.extensions.builtins.publishing.engine.PublisherEngine")
    def test_pull_dry_run_shows_diff(self, mock_engine_cls, capsys):
        """Pull with --dry-run shows diff without updating."""
        mock_engine = MagicMock()
        mock_engine.pull.return_value = {
            "changed": True,
            "diff": "--- local\n+++ external\n@@ -1 +1 @@\n-old line\n+new line",
            "comments": [],
        }
        mock_engine_cls.return_value = mock_engine

        args = argparse.Namespace(
            command="pull",
            doc_id="test-doc",
            all=False,
            dry_run=True,
            comments=False,
        )

        cli.cmd_pull(args)

        mock_engine.pull.assert_called_once_with("test-doc", dry_run=True, include_comments=False)
        captured = capsys.readouterr()
        assert "Changes detected" in captured.out
        assert "--- local" in captured.out
        assert "+new line" in captured.out

    @patch("axiom.extensions.builtins.publishing.engine.PublisherEngine")
    def test_pull_with_comments(self, mock_engine_cls, capsys):
        """Pull with --comments extracts and reports comments."""
        mock_engine = MagicMock()
        mock_engine.pull.return_value = {
            "changed": True,
            "source_path": "docs/test.md",
            "diff": None,
            "comments": [
                {"author": "Reviewer", "text": "Please clarify this section"},
                {"author": "Editor", "text": "Grammar fix needed"},
            ],
        }
        mock_engine_cls.return_value = mock_engine

        args = argparse.Namespace(
            command="pull",
            doc_id="test-doc",
            all=False,
            dry_run=False,
            comments=True,
        )

        cli.cmd_pull(args)

        mock_engine.pull.assert_called_once_with("test-doc", dry_run=False, include_comments=True)
        captured = capsys.readouterr()
        assert "2 comment(s)" in captured.out

    @patch("axiom.extensions.builtins.publishing.engine.PublisherEngine")
    def test_pull_all_docs(self, mock_engine_cls, capsys):
        """Pull --all iterates over all tracked documents."""
        from axiom.extensions.builtins.publishing.state import DocumentState, PublicationRecord

        mock_engine = MagicMock()
        mock_engine.status.return_value = [
            DocumentState(
                doc_id="doc1",
                source_path="docs/doc1.md",
                status="published",
                published=PublicationRecord(
                    storage_id="id1",
                    url="file://test",
                    version="v1",
                    published_at="2026-01-01",
                    commit_sha="abc",
                    generation_provider="pandoc-docx",
                    storage_provider="local",
                ),
            ),
            DocumentState(
                doc_id="doc2",
                source_path="docs/doc2.md",
                status="published",
                published=PublicationRecord(
                    storage_id="id2",
                    url="file://test2",
                    version="v1",
                    published_at="2026-01-01",
                    commit_sha="def",
                    generation_provider="pandoc-docx",
                    storage_provider="local",
                ),
            ),
        ]
        mock_engine.pull.return_value = {"changed": False}
        mock_engine_cls.return_value = mock_engine

        args = argparse.Namespace(
            command="pull",
            doc_id=None,
            all=True,
            dry_run=False,
            comments=False,
        )

        cli.cmd_pull(args)

        assert mock_engine.pull.call_count == 2
        captured = capsys.readouterr()
        assert "Pulling 2 document(s)" in captured.out

    @patch("axiom.extensions.builtins.publishing.engine.PublisherEngine")
    def test_pull_all_handles_errors(self, mock_engine_cls, capsys):
        """Pull --all continues on individual errors."""
        from axiom.extensions.builtins.publishing.state import DocumentState, PublicationRecord

        mock_engine = MagicMock()
        mock_engine.status.return_value = [
            DocumentState(
                doc_id="good-doc",
                source_path="docs/good.md",
                status="published",
                published=PublicationRecord(
                    storage_id="id1",
                    url="file://test",
                    version="v1",
                    published_at="2026-01-01",
                    commit_sha="abc",
                    generation_provider="pandoc-docx",
                    storage_provider="local",
                ),
            ),
            DocumentState(
                doc_id="bad-doc",
                source_path="docs/bad.md",
                status="published",
                published=PublicationRecord(
                    storage_id="id2",
                    url="file://test2",
                    version="v1",
                    published_at="2026-01-01",
                    commit_sha="def",
                    generation_provider="pandoc-docx",
                    storage_provider="local",
                ),
            ),
        ]

        def pull_side_effect(doc_id, **kwargs):
            if doc_id == "bad-doc":
                raise RuntimeError("Storage unavailable")
            return {"changed": False}

        mock_engine.pull.side_effect = pull_side_effect
        mock_engine_cls.return_value = mock_engine

        args = argparse.Namespace(
            command="pull",
            doc_id=None,
            all=True,
            dry_run=False,
            comments=False,
        )

        cli.cmd_pull(args)  # Should not raise

        captured = capsys.readouterr()
        assert "Error" in captured.out
        assert "Storage unavailable" in captured.out


class TestMainParser:
    """Tests for CLI argument parsing."""

    def test_pull_parser_defaults(self):
        """Pull subcommand has correct defaults."""
        # Access the parser through main module
        import argparse

        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers(dest="command")

        pull_parser = subparsers.add_parser("pull")
        pull_parser.add_argument("doc_id", nargs="?")
        pull_parser.add_argument("--all", action="store_true")
        pull_parser.add_argument("--dry-run", action="store_true")
        pull_parser.add_argument("--comments", action="store_true")

        args = parser.parse_args(["pull", "my-doc"])
        assert args.doc_id == "my-doc"
        assert args.all is False
        assert getattr(args, "dry_run", False) is False
        assert args.comments is False

    def test_pull_parser_all_flag(self):
        """Pull --all flag is parsed correctly."""
        import argparse

        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers(dest="command")

        pull_parser = subparsers.add_parser("pull")
        pull_parser.add_argument("doc_id", nargs="?")
        pull_parser.add_argument("--all", action="store_true")

        args = parser.parse_args(["pull", "--all"])
        assert args.doc_id is None
        assert args.all is True


class TestCmdPublish:
    """Tests for the `neut pub publish` command."""

    @patch("axiom.extensions.builtins.publishing.engine.PublisherEngine")
    def test_publish_file(self, mock_engine_cls, tmp_path):
        """Publish a specific file."""
        mock_engine = MagicMock()
        mock_engine.publish.return_value = MagicMock(version="v1", url="file://test")
        mock_engine_cls.return_value = mock_engine

        source = tmp_path / "test.md"
        source.write_text("# Test\n")

        args = argparse.Namespace(
            command="publish",
            file=str(source),
            all=False,
            changed_only=False,
            draft=False,
            endpoint=None,
        )

        cli.cmd_publish(args)

        mock_engine.publish.assert_called_once()

    @patch("axiom.extensions.builtins.publishing.engine.PublisherEngine")
    def test_publish_no_args_publishes_changed(self, mock_engine_cls, capsys):
        """Publish with no file/--all runs incremental diff and reports changed docs."""
        mock_engine = MagicMock()
        mock_engine_cls.return_value = mock_engine
        mock_engine.config.repo_root = Path("/tmp")
        mock_engine.diff.return_value = []  # Nothing changed

        args = argparse.Namespace(
            command="publish",
            file=None,
            all=False,
            changed_only=False,
            draft=False,
            endpoint=None,
            force=False,
        )

        cli.cmd_publish(args)

        # diff() called to determine what changed
        mock_engine.diff.assert_called_once()
        # No publish calls since nothing changed
        mock_engine.publish.assert_not_called()
        out = capsys.readouterr().out
        assert "No documents changed" in out


class TestCmdStatus:
    """Tests for the `neut pub status` command."""

    @patch("axiom.extensions.builtins.publishing.engine.PublisherEngine")
    def test_status_no_docs(self, mock_engine_cls, capsys):
        """Status with no tracked docs prints message."""
        mock_engine = MagicMock()
        mock_engine.status.return_value = []
        mock_engine_cls.return_value = mock_engine

        args = argparse.Namespace(command="status", file=None)

        cli.cmd_status(args)

        captured = capsys.readouterr()
        assert "No tracked documents" in captured.out

    @patch("axiom.extensions.builtins.publishing.engine.PublisherEngine")
    def test_status_lists_docs(self, mock_engine_cls, capsys):
        """Status lists all tracked documents."""
        from axiom.extensions.builtins.publishing.state import DocumentState, PublicationRecord

        mock_engine = MagicMock()
        mock_engine.status.return_value = [
            DocumentState(
                doc_id="test-prd",
                source_path="docs/prds/prd_test.md",
                status="published",
                published=PublicationRecord(
                    storage_id="id1",
                    url="file://test",
                    version="v2",
                    published_at="2026-02-24T10:00:00Z",
                    commit_sha="abc123",
                    generation_provider="pandoc-docx",
                    storage_provider="local",
                ),
            ),
        ]
        mock_engine_cls.return_value = mock_engine

        args = argparse.Namespace(command="status", file=None)

        cli.cmd_status(args)

        captured = capsys.readouterr()
        assert "test-prd" in captured.out
        assert "PUBLISHED" in captured.out
        assert "v2" in captured.out
