# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for BoxBrowserStorageProvider."""

from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest


class TestBoxBrowserStorage:

    def test_has_session_false_when_no_state(self, tmp_path: Path):
        from axiom.extensions.builtins.publishing.providers.storage.box_browser import (
            BoxBrowserStorageProvider,
        )
        provider = BoxBrowserStorageProvider({"session_dir": str(tmp_path / "session")})
        assert not provider.has_session()

    def test_has_session_true_when_state_exists(self, tmp_path: Path):
        from axiom.extensions.builtins.publishing.providers.storage.box_browser import (
            BoxBrowserStorageProvider,
        )
        session_dir = tmp_path / "session"
        session_dir.mkdir()
        (session_dir / "state.json").write_text("{}")
        provider = BoxBrowserStorageProvider({"session_dir": str(session_dir)})
        assert provider.has_session()

    @pytest.mark.integration
    @pytest.mark.skipif(
        not __import__("shutil").which("playwright"),
        reason="playwright not installed",
    )
    def test_upload_missing_file_returns_error(self, tmp_path: Path):
        from axiom.extensions.builtins.publishing.providers.storage.box_browser import (
            BoxBrowserStorageProvider,
        )
        provider = BoxBrowserStorageProvider({"session_dir": str(tmp_path / "session")})
        result = provider.upload(tmp_path / "nonexistent.docx")
        assert not result.success
        assert "not found" in result.error

    def test_clear_session(self, tmp_path: Path):
        from axiom.extensions.builtins.publishing.providers.storage.box_browser import (
            BoxBrowserStorageProvider,
        )
        session_dir = tmp_path / "session"
        session_dir.mkdir()
        (session_dir / "state.json").write_text("{}")
        provider = BoxBrowserStorageProvider({"session_dir": str(session_dir)})
        provider.clear_session()
        assert not provider.has_session()

    def test_needs_login_detects_box_urls(self):
        from axiom.extensions.builtins.publishing.providers.storage.box_browser import (
            BoxBrowserStorageProvider,
        )
        provider = BoxBrowserStorageProvider()
        mock_page = mock.MagicMock()

        mock_page.url = "https://account.box.com/login"
        assert provider._needs_login(mock_page)

        mock_page.url = "https://app.box.com/folder/0"
        assert not provider._needs_login(mock_page)

    def test_canonical_url(self):
        from axiom.extensions.builtins.publishing.providers.storage.box_browser import (
            BoxBrowserStorageProvider,
        )
        provider = BoxBrowserStorageProvider()
        assert provider.get_canonical_url("12345") == "https://app.box.com/file/12345"
        assert provider.get_canonical_url("") == ""

    def test_import_does_not_fail(self):
        from axiom.extensions.builtins.publishing.providers.storage import (
            box_browser,  # noqa: F401
        )
