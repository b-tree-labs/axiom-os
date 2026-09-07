# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for rag/watcher.py — no postgres, no real filesystem events.

Tests cover:
  - _DebounceMap: schedules callback, cancels previous on re-schedule
  - RAGWatchHandler.dispatch: ignores directories, ignores non-create/modify events
  - RAGWatchHandler._handle: routes sessions, signals, and regular files correctly
  - watch(): raises ImportError when watchdog not installed
"""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from axiom.rag.store import CORPUS_INTERNAL
from axiom.rag.watcher import RAGWatchHandler, _DebounceMap

# ---------------------------------------------------------------------------
# _DebounceMap
# ---------------------------------------------------------------------------

class TestDebounceMap:

    def test_schedules_callback(self):
        called = []
        dm = _DebounceMap(delay=0.05)
        dm.schedule("key", lambda: called.append(1))
        time.sleep(0.15)
        assert called == [1]

    def test_cancels_previous_on_reschedule(self):
        called = []
        dm = _DebounceMap(delay=0.1)
        dm.schedule("key", lambda: called.append("first"))
        dm.schedule("key", lambda: called.append("second"))  # cancels first
        time.sleep(0.25)
        assert called == ["second"]

    def test_independent_keys_both_fire(self):
        called = []
        dm = _DebounceMap(delay=0.05)
        dm.schedule("a", lambda: called.append("a"))
        dm.schedule("b", lambda: called.append("b"))
        time.sleep(0.2)
        assert set(called) == {"a", "b"}


# ---------------------------------------------------------------------------
# Fake watchdog event
# ---------------------------------------------------------------------------

def _event(src_path: str, event_type: str = "modified", is_directory: bool = False):
    e = MagicMock()
    e.src_path = src_path
    e.event_type = event_type
    e.is_directory = is_directory
    return e


# ---------------------------------------------------------------------------
# RAGWatchHandler.dispatch
# ---------------------------------------------------------------------------

class TestRAGWatchHandlerDispatch:

    def setup_method(self):
        self.store = MagicMock()
        self.repo_root = Path("/fake/repo")
        self.handler = RAGWatchHandler(self.store, self.repo_root, CORPUS_INTERNAL)
        # Patch debounce to call immediately
        self.handler._debounce.schedule = lambda key, fn: fn()

    def test_ignores_directory_events(self):
        with patch.object(self.handler, "_handle") as mock_handle:
            self.handler.dispatch(_event("/fake/repo/docs", is_directory=True))
            mock_handle.assert_not_called()

    def test_ignores_deleted_events(self):
        with patch.object(self.handler, "_handle") as mock_handle:
            self.handler.dispatch(_event("/fake/repo/docs/file.md", event_type="deleted"))
            mock_handle.assert_not_called()

    def test_handles_created_events(self):
        with patch.object(self.handler, "_handle") as mock_handle:
            self.handler.dispatch(_event("/fake/repo/docs/new.md", event_type="created"))
            mock_handle.assert_called_once()

    def test_handles_modified_events(self):
        with patch.object(self.handler, "_handle") as mock_handle:
            self.handler.dispatch(_event("/fake/repo/docs/file.md", event_type="modified"))
            mock_handle.assert_called_once()

    def test_passes_path_object_to_handle(self):
        captured = []
        self.handler._handle = captured.append  # type: ignore[assignment]
        self.handler.dispatch(_event("/fake/repo/docs/x.md", event_type="modified"))
        assert len(captured) == 1
        assert isinstance(captured[0], Path)
        assert captured[0] == Path("/fake/repo/docs/x.md")


# ---------------------------------------------------------------------------
# RAGWatchHandler._handle routing
# ---------------------------------------------------------------------------

class TestRAGWatchHandlerHandle:

    def setup_method(self):
        self.store = MagicMock()
        self.repo_root = Path("/fake/repo")
        self.handler = RAGWatchHandler(self.store, self.repo_root, CORPUS_INTERNAL)

    def _sessions_dir(self) -> Path:
        return self.repo_root / "runtime" / "sessions"

    def _signals_dir(self) -> Path:
        return self.repo_root / "runtime" / "inbox" / "processed"

    def test_routes_session_json_to_ingest_session_file(self):
        session_path = self._sessions_dir() / "abc.json"
        with patch("axiom.rag.personal.ingest_session_file", return_value=True) as mock_ingest:
            self.handler._handle(session_path)
        mock_ingest.assert_called_once_with(session_path, self.store, corpus=CORPUS_INTERNAL)

    def test_does_not_route_session_non_json(self):
        path = self._sessions_dir() / "readme.txt"
        with patch("axiom.rag.personal.ingest_session_file") as mock_ingest:
            with patch("axiom.rag.ingest.ingest_file"):
                self.handler._handle(path)
        mock_ingest.assert_not_called()

    def test_routes_signal_json_to_extract_upsert(self):
        signal_path = self._signals_dir() / "sig.json"
        with patch("axiom.rag.personal._extract_signal_text", return_value=("Title", "# Title\n\ncontent")) as mock_extract:
            with patch("axiom.rag.personal._upsert", return_value=True) as mock_upsert:
                self.handler._handle(signal_path)
        mock_extract.assert_called_once_with(signal_path)
        mock_upsert.assert_called_once()

    def test_routes_signal_json_skips_on_none_extract(self):
        signal_path = self._signals_dir() / "empty.json"
        with patch("axiom.rag.personal._extract_signal_text", return_value=None):
            with patch("axiom.rag.personal._upsert") as mock_upsert:
                self.handler._handle(signal_path)
        mock_upsert.assert_not_called()

    def test_routes_regular_file_to_ingest_file(self):
        doc_path = self.repo_root / "docs" / "spec.md"
        from axiom.rag.ingest import IngestStats
        fake_stats = IngestStats()
        fake_stats.files_indexed = 1

        with patch("axiom.rag.ingest.ingest_file", return_value=fake_stats) as mock_ingest:
            self.handler._handle(doc_path)

        mock_ingest.assert_called_once_with(
            doc_path, self.store,
            repo_root=self.repo_root,
            corpus=CORPUS_INTERNAL,
        )

    def test_handle_swallows_exceptions(self):
        doc_path = self.repo_root / "docs" / "bad.md"
        with patch("axiom.rag.ingest.ingest_file", side_effect=RuntimeError("boom")):
            # Should not raise
            self.handler._handle(doc_path)

    def test_signal_upsert_uses_signals_prefix(self):
        signal_path = self._signals_dir() / "my_signal.json"
        with patch("axiom.rag.personal._extract_signal_text", return_value=("T", "# T\n\ntext")):
            with patch("axiom.rag.personal._upsert") as mock_upsert:
                self.handler._handle(signal_path)

        source_path_arg = mock_upsert.call_args.args[0]
        assert source_path_arg == "signals/my_signal.json"


# ---------------------------------------------------------------------------
# watch() — import guard
# ---------------------------------------------------------------------------

def test_watch_raises_on_missing_watchdog(tmp_path):
    from axiom.rag.watcher import watch
    store = MagicMock()

    import builtins
    real_import = builtins.__import__

    def mock_import(name, *args, **kwargs):
        if name == "watchdog.observers":
            raise ImportError("No module named 'watchdog'")
        return real_import(name, *args, **kwargs)

    with patch("builtins.__import__", side_effect=mock_import):
        with pytest.raises(ImportError, match="watchdog"):
            watch(tmp_path, store, CORPUS_INTERNAL, quiet=True)
