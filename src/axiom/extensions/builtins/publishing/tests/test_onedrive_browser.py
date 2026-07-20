# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for OneDriveBrowserStorageProvider — Playwright-based OneDrive upload."""

from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest


def _has_playwright() -> bool:
    try:
        import playwright  # noqa: F401
        return True
    except ImportError:
        return False


class TestOneDriveBrowserStorage:

    def test_has_session_false_when_no_state(self, tmp_path: Path):
        from axiom.extensions.builtins.publishing.providers.storage.onedrive_browser import (
            OneDriveBrowserStorageProvider,
        )
        provider = OneDriveBrowserStorageProvider({"session_dir": str(tmp_path / "session")})
        assert not provider.has_session()

    def test_has_session_true_when_state_exists(self, tmp_path: Path):
        from axiom.extensions.builtins.publishing.providers.storage.onedrive_browser import (
            OneDriveBrowserStorageProvider,
        )
        session_dir = tmp_path / "session"
        session_dir.mkdir()
        (session_dir / "state.json").write_text("{}")
        provider = OneDriveBrowserStorageProvider({"session_dir": str(session_dir)})
        assert provider.has_session()

    @pytest.mark.skipif(
        not _has_playwright(), reason="playwright not installed",
    )
    def test_upload_missing_file_returns_error(self, tmp_path: Path):
        from axiom.extensions.builtins.publishing.providers.storage.onedrive_browser import (
            OneDriveBrowserStorageProvider,
        )
        provider = OneDriveBrowserStorageProvider({"session_dir": str(tmp_path / "session")})
        result = provider.upload(tmp_path / "nonexistent.docx")
        assert not result.success
        assert "not found" in result.error

    def test_clear_session(self, tmp_path: Path):
        from axiom.extensions.builtins.publishing.providers.storage.onedrive_browser import (
            OneDriveBrowserStorageProvider,
        )
        session_dir = tmp_path / "session"
        session_dir.mkdir()
        (session_dir / "state.json").write_text("{}")
        provider = OneDriveBrowserStorageProvider({"session_dir": str(session_dir)})
        provider.clear_session()
        assert not provider.has_session()

    def test_needs_login_detects_microsoft_urls(self):
        from axiom.extensions.builtins.publishing.providers.storage.onedrive_browser import (
            OneDriveBrowserStorageProvider,
        )
        provider = OneDriveBrowserStorageProvider()
        mock_page = mock.MagicMock()

        mock_page.url = "https://login.microsoftonline.com/common/oauth2"
        assert provider._needs_login(mock_page)

        mock_page.url = "https://onedrive.live.com/my-files"
        assert not provider._needs_login(mock_page)

    @pytest.mark.skipif(
        not _has_playwright(), reason="playwright not installed",
    )
    def test_batch_upload_returns_results_per_file(self, tmp_path: Path):
        from axiom.extensions.builtins.publishing.providers.storage.onedrive_browser import (
            OneDriveBrowserStorageProvider,
        )
        provider = OneDriveBrowserStorageProvider({"session_dir": str(tmp_path / "session")})

        # Mock ensure_playwright to avoid import check
        with mock.patch.object(provider, "_ensure_playwright"):
            # All files missing → all results are errors
            files = [tmp_path / "a.docx", tmp_path / "b.docx"]
            with mock.patch("playwright.sync_api.sync_playwright") as mock_pw:
                mock_context = mock.MagicMock()
                mock_browser = mock.MagicMock()
                mock_browser.new_context.return_value = mock_context
                mock_context.new_page.return_value = mock.MagicMock()
                mock_context.storage_state = mock.MagicMock()
                mock_pw.return_value.__enter__ = mock.MagicMock(return_value=mock.MagicMock(chromium=mock.MagicMock(launch=mock.MagicMock(return_value=mock_browser))))
                mock_pw.return_value.__exit__ = mock.MagicMock(return_value=False)

                results = provider.upload_batch(files)

        assert len(results) == 2
        assert all(not r.success for r in results)

    def test_import_does_not_fail(self):
        from axiom.extensions.builtins.publishing.providers.storage import (
            onedrive_browser,  # noqa: F401
        )
