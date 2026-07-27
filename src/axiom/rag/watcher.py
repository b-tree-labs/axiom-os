# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Filesystem watcher that keeps the personal RAG corpus up-to-date.

Watches four directories and incrementally re-indexes any file that is
created or modified, without requiring a full ``axi rag index`` run.

Usage::

    axi rag watch          # foreground; Ctrl-C to stop
    axi rag watch --daemon # not yet implemented — use a launchd plist

Watched paths (relative to REPO_ROOT):
    docs/                         → rag-internal (project knowledge)
    runtime/knowledge/            → rag-internal (external docs drop-zone)
    runtime/sessions/*.json       → rag-internal (chat transcripts)
    runtime/inbox/processed/      → rag-internal (sense signals)

Requires ``watchdog`` (pip install watchdog).
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from threading import Timer

log = logging.getLogger(__name__)

# Debounce window — coalesce rapid writes (e.g. editor temp-file swaps)
_DEBOUNCE_SECONDS = 2.0


class _DebounceMap:
    """Fire a callback at most once per debounce window per path."""

    def __init__(self, delay: float = _DEBOUNCE_SECONDS) -> None:
        self._delay = delay
        self._timers: dict[str, Timer] = {}

    def schedule(self, key: str, fn) -> None:
        if key in self._timers:
            self._timers[key].cancel()
        t = Timer(self._delay, fn)
        t.daemon = True
        t.start()
        self._timers[key] = t


class RAGWatchHandler:
    """watchdog event handler — calls the right ingest function per path."""

    def __init__(self, store, repo_root: Path, corpus: str) -> None:
        self._store = store
        self._repo_root = repo_root
        self._corpus = corpus
        self._debounce = _DebounceMap()

    # -- watchdog interface --------------------------------------------------

    def dispatch(self, event) -> None:
        """Called by the watchdog Observer for every filesystem event."""
        if event.is_directory:
            return
        # Only care about creation and modification
        if event.event_type not in ("created", "modified"):
            return
        path = Path(event.src_path)
        self._debounce.schedule(str(path), lambda p=path: self._handle(p))

    def _handle(self, path: Path) -> None:
        """Route to the right ingest function based on where the file lives."""
        from axiom.rag.ingest import ingest_file
        from axiom.rag.personal import ingest_session_file

        sessions_dir = self._repo_root / "runtime" / "sessions"
        signals_dir = self._repo_root / "runtime" / "inbox" / "processed"

        try:
            if path.is_relative_to(sessions_dir) and path.suffix == ".json":
                ok = ingest_session_file(path, self._store, corpus=self._corpus)
                if ok:
                    log.info("[watch] Indexed session: %s", path.name)

            elif path.is_relative_to(signals_dir) and path.suffix == ".json":
                from axiom.rag.personal import _extract_signal_text, _upsert
                result = _extract_signal_text(path)
                if result:
                    title, text = result
                    source_path = f"signals/{path.name}"
                    ok = _upsert(source_path, title, "signal", text,
                                 self._store, self._corpus)
                    if ok:
                        log.info("[watch] Indexed signal: %s", path.name)

            else:
                # Regular file — use standard ingest_file (checks extensions)
                stats = ingest_file(
                    path, self._store,
                    repo_root=self._repo_root,
                    corpus=self._corpus,
                )
                if stats.files_indexed:
                    log.info("[watch] Indexed: %s", path.name)

        except Exception as exc:
            log.warning("[watch] Error indexing %s: %s", path, exc)


def watch(
    repo_root: Path,
    store,
    corpus: str,
    quiet: bool = False,
) -> None:
    """Block forever, watching repo_root and re-indexing changed files.

    Raises ``ImportError`` if ``watchdog`` is not installed.
    """
    try:
        from watchdog.events import FileSystemEventHandler
        from watchdog.observers import Observer
    except ImportError:
        raise ImportError(
            "watchdog is required for 'axi rag watch'.\n"
            "Install it: pip install watchdog"
        )

    handler = RAGWatchHandler(store, repo_root, corpus)

    # Wrap our handler in a watchdog-compatible shim
    class _Shim(FileSystemEventHandler):
        def dispatch(self, event):
            handler.dispatch(event)

    observer = Observer()
    watch_dirs = [
        repo_root / "docs",
        repo_root / "runtime" / "knowledge",
        repo_root / "runtime" / "sessions",
        repo_root / "runtime" / "inbox" / "processed",
    ]

    scheduled = []
    for d in watch_dirs:
        d.mkdir(parents=True, exist_ok=True)
        observer.schedule(_Shim(), str(d), recursive=True)
        scheduled.append(d)

    observer.start()

    if not quiet:
        print(f"Watching {len(scheduled)} directories for changes. Ctrl-C to stop.")
        for d in scheduled:
            rel = d.relative_to(repo_root)
            print(f"  {rel}/")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        observer.stop()
        observer.join()
        if not quiet:
            print("Watcher stopped.")
