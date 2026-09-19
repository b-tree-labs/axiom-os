# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Background version checker for the platform.

Runs a daemon thread that periodically checks for updates and fires a
callback when a newer version is found.  Thread-safe — the callback must
only use thread-safe methods (e.g. FullScreenChat._append_output).
"""

from __future__ import annotations

import threading
from collections.abc import Callable

from .version_check import VersionChecker, VersionInfo

_INITIAL_DELAY = 10.0   # seconds before first check
_CHECK_INTERVAL = 3600.0  # seconds between checks (1 hour)


class BackgroundUpdateChecker:
    """Daemon thread that periodically checks for remote updates."""

    def __init__(
        self,
        on_update_available: Callable[[VersionInfo], None] | None = None,
        initial_delay: float = _INITIAL_DELAY,
        interval: float = _CHECK_INTERVAL,
    ):
        self._on_update_available = on_update_available
        self._initial_delay = initial_delay
        self._interval = interval
        self._checker = VersionChecker()
        self._latest: VersionInfo | None = None
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Start the background check loop."""
        if self._thread is not None:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._loop,
            daemon=True,
            name="update-checker",
        )
        self._thread.start()

    def stop(self) -> None:
        """Signal the background thread to stop."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    @property
    def latest(self) -> VersionInfo | None:
        """Most recent check result (thread-safe read)."""
        return self._latest

    def check_now(self) -> VersionInfo | None:
        """Run a synchronous check immediately (blocking)."""
        try:
            info = self._checker.check_remote_version(timeout=5.0)
            self._latest = info
            if info.is_newer and self._on_update_available:
                self._on_update_available(info)
            return info
        except Exception:
            return self._latest

    def _loop(self) -> None:
        """Background loop: initial delay, then check + sleep."""
        # Initial delay to let TUI initialize and first LLM call complete
        if self._stop_event.wait(timeout=self._initial_delay):
            return  # stopped during initial delay

        while not self._stop_event.is_set():
            try:
                info = self._checker.check_remote_version(timeout=5.0)
                self._latest = info
                if info.is_newer and self._on_update_available:
                    self._on_update_available(info)
            except Exception:
                pass  # Degrade silently when offline

            # Wait for next interval or stop signal
            if self._stop_event.wait(timeout=self._interval):
                break
