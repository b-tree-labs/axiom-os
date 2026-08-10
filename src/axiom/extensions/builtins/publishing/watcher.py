# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""PRESS file watcher — publish-on-save daemon.

Watches source_dirs for markdown changes and auto-publishes after
the cooldown settles. Runs as a background thread or standalone daemon.

Usage:
    axi pub watch              # Watch and auto-publish (foreground)
    axi pub watch --daemon     # Run in background via ServiceManager

Architecture:
    - Uses polling (not inotify/fsevents) for cross-platform simplicity
    - Respects publisher.cooldown_seconds (default: 300s / 5 min)
    - Debounces: tracks last-modified time per file, publishes only
      after no changes for cooldown duration
    - Integrates with existing PublisherEngine.publish()
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_DEFAULT_POLL_INTERVAL = 10  # seconds between scans
_DEFAULT_COOLDOWN = 300  # seconds to wait after last edit before publishing


class PublishWatcher:
    """Watches source directories and auto-publishes changed docs."""

    def __init__(
        self,
        source_dirs: list[dict[str, str]] | None = None,
        poll_interval: int = _DEFAULT_POLL_INTERVAL,
        cooldown: int = _DEFAULT_COOLDOWN,
    ):
        self._source_dirs = source_dirs or self._load_source_dirs()
        self._poll_interval = poll_interval
        self._cooldown = cooldown

        # Track file state: path → {mtime, last_changed_at, published}
        self._file_state: dict[str, dict[str, Any]] = {}
        self._running = False

    @staticmethod
    def _load_source_dirs() -> list[dict[str, str]]:
        """Load source_dirs from workflow.yaml or use defaults."""
        from axiom import REPO_ROOT
        from axiom.infra.config_loader import load_yaml

        for config_name in [".neut/publisher/workflow.yaml", ".publisher.yaml"]:
            config_path = REPO_ROOT / config_name
            cfg = load_yaml(config_path)
            dirs = cfg.get("source_dirs", cfg.get("folders", []))
            if dirs:
                return dirs

        return [
            {"path": "docs/prds", "pattern": "*.md"},
            {"path": "docs/adrs", "pattern": "*.md"},
            {"path": "docs/specs", "pattern": "*.md"},
        ]

    def _scan_files(self) -> dict[str, float]:
        """Scan source_dirs and return path → mtime mapping."""
        from axiom import REPO_ROOT

        files: dict[str, float] = {}
        for dir_cfg in self._source_dirs:
            folder = REPO_ROOT / dir_cfg["path"]
            pattern = dir_cfg.get("pattern", "*.md")
            if not folder.exists():
                continue
            for f in folder.glob(pattern):
                if f.name.startswith("_") or f.name == "README.md":
                    continue
                files[str(f)] = f.stat().st_mtime
        return files

    def _check_and_publish(self) -> int:
        """Check for settled changes and publish. Returns count published."""
        from axiom.extensions.builtins.publishing.engine import PublisherEngine

        now = time.time()
        current_files = self._scan_files()
        published_count = 0

        for path_str, mtime in current_files.items():
            state = self._file_state.get(path_str)

            if state is None:
                # First scan — record state, don't publish
                self._file_state[path_str] = {
                    "mtime": mtime,
                    "last_changed_at": now,
                    "published": False,
                }
                continue

            if mtime != state["mtime"]:
                # File changed — reset the cooldown timer
                state["mtime"] = mtime
                state["last_changed_at"] = now
                state["published"] = False
                log.debug("Change detected: %s", Path(path_str).name)
                continue

            if state["published"]:
                continue  # Already published this version

            # File hasn't changed — check if cooldown has elapsed
            settled_for = now - state["last_changed_at"]
            if settled_for >= self._cooldown:
                # Cooldown elapsed — publish
                path = Path(path_str)
                try:
                    engine = PublisherEngine()
                    result = engine.publish(path)
                    if result:
                        state["published"] = True
                        published_count += 1
                        ts = datetime.now().strftime("%H:%M:%S")
                        print(f"  [{ts}] Published: {path.name}")
                except Exception as e:
                    log.warning("Auto-publish failed for %s: %s", path.name, e)

        return published_count

    def run(self) -> None:
        """Run the watcher loop (blocking)."""
        print("\n  PRESS Watcher")
        print(f"  Watching {len(self._source_dirs)} source director(ies)")
        print(f"  Poll: {self._poll_interval}s | Cooldown: {self._cooldown}s")
        print("  Press Ctrl+C to stop\n")

        # Initial scan (populate state without publishing)
        self._scan_files()
        for path_str, mtime in self._scan_files().items():
            self._file_state[path_str] = {
                "mtime": mtime,
                "last_changed_at": 0,  # Won't trigger publish (cooldown from epoch)
                "published": True,  # Assume current state is published
            }

        file_count = len(self._file_state)
        print(f"  Tracking {file_count} file(s). Waiting for changes...\n")

        self._running = True
        try:
            while self._running:
                count = self._check_and_publish()
                if count > 0:
                    print(f"  ({count} doc(s) published)\n")
                time.sleep(self._poll_interval)
        except KeyboardInterrupt:
            print("\n  Watcher stopped.")

    def stop(self) -> None:
        """Stop the watcher loop."""
        self._running = False


# ---------------------------------------------------------------------------
# Singleton for daemon mode (TIDY heartbeat calls this)
# ---------------------------------------------------------------------------

_daemon_watcher: PublishWatcher | None = None


def run_watcher_cycle() -> int:
    """Run a single watcher poll cycle. Called by TIDY on each heartbeat.

    Returns the number of documents published (0 if nothing changed).
    Initializes the watcher on first call. Stateful across calls —
    tracks file mtimes and cooldowns between heartbeats.
    """
    global _daemon_watcher
    if _daemon_watcher is None:
        cooldown = 300
        try:
            from axiom.extensions.builtins.settings.store import SettingsStore
            cooldown = int(SettingsStore().get("publisher.cooldown_seconds", 300))
        except Exception:
            pass
        _daemon_watcher = PublishWatcher(cooldown=cooldown)
        # Initial scan — populate state without publishing
        for path_str, mtime in _daemon_watcher._scan_files().items():
            _daemon_watcher._file_state[path_str] = {
                "mtime": mtime,
                "last_changed_at": 0,
                "published": True,
            }
        log.info("PRESS watcher initialized: tracking %d files", len(_daemon_watcher._file_state))

    return _daemon_watcher._check_and_publish()
