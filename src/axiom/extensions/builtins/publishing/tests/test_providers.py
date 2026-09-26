# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Contract tests for Publisher providers.

These tests verify that provider implementations satisfy their ABC contracts.
They test the interface, not implementation details.
"""

import pytest

from axiom.extensions.builtins.publishing.providers.base import (
    FeedbackProvider,
    GenerationOptions,
    GenerationProvider,
    NotificationProvider,
    StorageProvider,
)

# ─── Storage Provider Contract Tests ───


class TestLocalStorageContract:
    """Verify LocalStorageProvider satisfies the StorageProvider contract."""

    @pytest.fixture
    def provider(self, tmp_path):
        from axiom.extensions.builtins.publishing.providers.storage.local import (
            LocalStorageProvider,
        )

        return LocalStorageProvider({"base_dir": str(tmp_path / "output")})

    @pytest.fixture
    def sample_file(self, tmp_path):
        f = tmp_path / "test.docx"
        f.write_text("fake docx content")
        return f

    def test_is_storage_provider(self, provider):
        assert isinstance(provider, StorageProvider)

    def test_upload(self, provider, sample_file):
        result = provider.upload(sample_file, "test.docx", {"version": "v1"})
        assert result.storage_id
        assert result.canonical_url.startswith("file://")
        assert result.version == "v1"

    def test_upload_creates_parent_dirs(self, provider, sample_file):
        result = provider.upload(sample_file, "specs/deep/test.docx", {})
        assert result.storage_id == "specs/deep/test.docx"

    def test_download(self, provider, sample_file, tmp_path):
        result = provider.upload(sample_file, "dl-test.docx", {})

        download_path = tmp_path / "downloaded.docx"
        provider.download(result.storage_id, download_path)
        assert download_path.exists()
        assert download_path.read_text() == "fake docx content"

    def test_download_nonexistent(self, provider, tmp_path):
        with pytest.raises(FileNotFoundError):
            provider.download("nonexistent.docx", tmp_path / "out.docx")

    def test_get_canonical_url(self, provider, sample_file):
        result = provider.upload(sample_file, "url-test.docx", {})
        url = provider.get_canonical_url(result.storage_id)
        assert url.startswith("file://")

    def test_list_artifacts(self, provider, sample_file):
        provider.upload(sample_file, "a.docx", {})
        provider.upload(sample_file, "b.docx", {})

        entries = provider.list_artifacts("")
        assert len(entries) >= 2

    def test_delete(self, provider, sample_file):
        result = provider.upload(sample_file, "delete-me.docx", {})
        assert provider.delete(result.storage_id) is True
        assert provider.delete(result.storage_id) is False  # Already deleted

    def test_move(self, provider, sample_file):
        result = provider.upload(sample_file, "before.docx", {})
        moved = provider.move(result.storage_id, "after.docx")
        assert moved.storage_id == "after.docx"

    def test_move_nonexistent(self, provider):
        with pytest.raises(FileNotFoundError):
            provider.move("ghost.docx", "new.docx")


# ─── Notification Provider Contract Tests ───


class TestTerminalNotificationContract:
    """Verify TerminalNotificationProvider satisfies the contract."""

    @pytest.fixture
    def provider(self):
        from axiom.extensions.builtins.publishing.providers.notification.terminal import (
            TerminalNotificationProvider,
        )

        return TerminalNotificationProvider({})

    def test_is_notification_provider(self, provider):
        assert isinstance(provider, NotificationProvider)

    def test_send_returns_true(self, provider):
        result = provider.send(
            recipients=["alice"],
            subject="Test notification",
            body="This is a test",
        )
        assert result is True

    def test_send_with_urgency(self, provider, capsys):
        provider.send(
            recipients=[],
            subject="Urgent!",
            body="Something happened",
            urgency="high",
        )
        output = capsys.readouterr().out
        assert "Urgent!" in output
        assert "!" in output  # High urgency indicator


# ─── Feedback Provider Contract Tests ───


class TestDocxFeedbackContract:
    """Verify DocxFeedbackProvider satisfies the contract."""

    @pytest.fixture
    def provider(self):
        from axiom.extensions.builtins.publishing.providers.feedback.docx_comments import (
            DocxFeedbackProvider,
        )

        return DocxFeedbackProvider({})

    def test_is_feedback_provider(self, provider):
        assert isinstance(provider, FeedbackProvider)

    def test_supports_inline_comments(self, provider):
        assert provider.supports_inline_comments() is True

    def test_fetch_from_nonexistent_file(self, provider, tmp_path):
        comments = provider.fetch_comments(str(tmp_path / "nonexistent" / "path.docx"))
        assert comments == []

    def test_fetch_from_non_docx(self, provider, tmp_path):
        txt = tmp_path / "notes.txt"
        txt.write_text("not a docx")
        comments = provider.fetch_comments(str(txt))
        assert comments == []

    def test_mark_resolved_returns_false(self, provider):
        # Phase 1: mark_resolved is not supported
        assert provider.mark_resolved("ref", "id") is False


# ─── Generation Provider Contract Tests ───


class TestPandocDocxContract:
    """Verify PandocDocxProvider satisfies the contract."""

    @pytest.fixture
    def provider(self):
        from axiom.extensions.builtins.publishing.providers.generation.pandoc_docx import (
            PandocDocxProvider,
        )

        return PandocDocxProvider({})

    def test_is_generation_provider(self, provider):
        assert isinstance(provider, GenerationProvider)

    def test_get_output_extension(self, provider):
        assert provider.get_output_extension() == ".docx"

    def test_supports_watermark(self, provider):
        assert provider.supports_watermark() is True

    def test_generate_from_markdown(self, provider, tmp_path, repo_root):
        """Generate a .docx from a small markdown file."""
        source = tmp_path / "test.md"
        source.write_text("# Test Document\n\nThis is a test.\n\n## Section 2\n\nMore content.\n")

        output = tmp_path / "test.docx"
        options = GenerationOptions(toc=False)
        result = provider.generate(source, output, options)

        assert result.output_path.exists()
        assert result.format == "docx"
        assert result.size_bytes > 0

    def test_rewrite_links_noop_empty_map(self, provider, tmp_path):
        """rewrite_links with empty map doesn't crash."""
        f = tmp_path / "test.docx"
        f.write_bytes(b"not a real docx")
        # Should not raise
        provider.rewrite_links(f, {})
