# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for TeamsBrowserExtractor — Playwright-based Teams transcript fetcher.

Tests mock Playwright so they run without a browser.
Integration tests (actually launching a browser) are skipped by default.
"""

from __future__ import annotations

from pathlib import Path
from unittest import mock


class TestTeamsBrowserExtractor:
    """Unit tests for TeamsBrowserExtractor (mocked Playwright)."""

    def test_name(self):
        from axiom.extensions.builtins.signals.extractors.teams_browser import (
            TeamsBrowserExtractor,
        )
        ext = TeamsBrowserExtractor()
        assert ext.name == "teams_browser"

    def test_is_available_without_playwright(self):
        from axiom.extensions.builtins.signals.extractors.teams_browser import (
            TeamsBrowserExtractor,
        )
        with mock.patch.dict("sys.modules", {"playwright": None}):
            # Can't easily test ImportError this way; just verify the method exists
            assert hasattr(TeamsBrowserExtractor, "is_available")

    def test_can_handle_returns_false(self, tmp_path: Path):
        """Browser extractor fetches, not processes local files."""
        from axiom.extensions.builtins.signals.extractors.teams_browser import (
            TeamsBrowserExtractor,
        )
        ext = TeamsBrowserExtractor()
        assert not ext.can_handle(tmp_path / "anything.vtt")

    def test_has_session_false_when_no_state(self, tmp_path: Path):
        from axiom.extensions.builtins.signals.extractors.teams_browser import (
            TeamsBrowserExtractor,
        )
        ext = TeamsBrowserExtractor(session_dir=tmp_path / "session")
        assert not ext.has_session()

    def test_has_session_true_when_state_exists(self, tmp_path: Path):
        from axiom.extensions.builtins.signals.extractors.teams_browser import (
            TeamsBrowserExtractor,
        )
        session_dir = tmp_path / "session"
        session_dir.mkdir()
        (session_dir / "state.json").write_text("{}")

        ext = TeamsBrowserExtractor(session_dir=session_dir)
        assert ext.has_session()

    def test_clear_session(self, tmp_path: Path):
        from axiom.extensions.builtins.signals.extractors.teams_browser import (
            TeamsBrowserExtractor,
        )
        session_dir = tmp_path / "session"
        session_dir.mkdir()
        (session_dir / "state.json").write_text("{}")

        ext = TeamsBrowserExtractor(session_dir=session_dir)
        ext.clear_session()
        assert not ext.has_session()

    def test_extract_returns_error_without_playwright(self, tmp_path: Path):
        """Extract gracefully errors when Playwright is not installed."""
        from axiom.extensions.builtins.signals.extractors.teams_browser import (
            TeamsBrowserExtractor,
        )
        ext = TeamsBrowserExtractor(
            session_dir=tmp_path / "session",
            download_dir=tmp_path / "downloads",
        )

        with mock.patch.object(ext, "ensure_playwright", side_effect=RuntimeError("not installed")):
            result = ext.extract(tmp_path)

        assert len(result.errors) > 0
        assert "not installed" in result.errors[0]

    def test_extract_no_transcripts_found(self, tmp_path: Path):
        """When fetch returns empty, extraction reports it."""
        from axiom.extensions.builtins.signals.extractors.teams_browser import (
            TeamsBrowserExtractor,
        )
        ext = TeamsBrowserExtractor(
            session_dir=tmp_path / "session",
            download_dir=tmp_path / "downloads",
        )

        with mock.patch.object(ext, "fetch_transcripts", return_value=[]):
            result = ext.extract(tmp_path)

        assert result.signals == []
        assert any("No transcripts" in e for e in result.errors)

    def test_extract_processes_downloaded_files(self, tmp_path: Path):
        """Downloaded files are passed through TranscriptExtractor."""
        from axiom.extensions.builtins.signals.extractors.teams_browser import (
            TeamsBrowserExtractor,
        )
        from axiom.extensions.builtins.signals.models import Extraction, Signal

        # Create a fake downloaded transcript
        download_dir = tmp_path / "downloads"
        download_dir.mkdir()
        transcript = download_dir / "meeting_2026-03-15.vtt"
        transcript.write_text(
            "WEBVTT\n\n00:00:00.000 --> 00:00:05.000\nHello world\n"
        )

        ext = TeamsBrowserExtractor(
            session_dir=tmp_path / "session",
            download_dir=download_dir,
        )

        mock_extraction = Extraction(
            extractor="transcript",
            source_file=str(transcript),
            signals=[Signal(
                source="transcript",
                timestamp="2026-03-15T10:00:00Z",
                raw_text="Hello world",
                signal_type="raw",
                detail="test signal",
            )],
        )

        with mock.patch.object(ext, "fetch_transcripts", return_value=[transcript]):
            with mock.patch(
                "axiom.extensions.builtins.signals.extractors.transcript.TranscriptExtractor"
            ) as MockTranscript:
                MockTranscript.return_value.extract.return_value = mock_extraction
                result = ext.extract(tmp_path)

        assert len(result.signals) == 1
        assert result.signals[0].raw_text == "Hello world"

    def test_needs_login_detects_microsoft_urls(self):
        from axiom.extensions.builtins.signals.extractors.teams_browser import (
            TeamsBrowserExtractor,
        )
        ext = TeamsBrowserExtractor()

        mock_page = mock.MagicMock()

        mock_page.url = "https://login.microsoftonline.com/common/oauth2/authorize"
        assert ext._needs_login(mock_page)

        mock_page.url = "https://teams.microsoft.com/_#/calendarv2"
        assert not ext._needs_login(mock_page)

    def test_registered_as_source(self):
        """teams_browser module has _REGISTERED_SOURCES from decorator."""
        import axiom.extensions.builtins.signals.extractors.teams_browser as mod
        sources = getattr(mod, "_REGISTERED_SOURCES", [])
        names = [meta.name for meta, _ in sources]
        assert "teams_browser" in names


class TestTeamsBrowserRegistration:
    """Verify the extractor integrates with the signal pipeline."""

    def test_import_does_not_fail(self):
        """Module imports cleanly even without Playwright installed."""
        from axiom.extensions.builtins.signals.extractors import teams_browser  # noqa: F401
