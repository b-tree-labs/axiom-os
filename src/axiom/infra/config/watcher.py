# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Filesystem watcher for ``axiom.infra.config``.

Watches the config directory for ``*.toml`` changes; on detect, parses
the file + applies the values to the registry. Validation failures don't
take effect (the lesson from NixOS atomic generations). Cross-platform
via the ``watchdog`` library — Linux inotify, macOS FSEvents, Windows
ReadDirectoryChangesW.

A polling fallback exists for environments without ``watchdog``
(constrained self-hosted nodes, locked-down Windows installs). Both
implement the same ``Watcher`` protocol so callers don't care which
backend is in use.

Per AEOS §2.13 + ADR-058: this is the watcher Ben said the core runtime
MUST ship by default. Extensions never roll their own.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

log = logging.getLogger("axiom.infra.config.watcher")


# ---------------------------------------------------------------------------
# File loading
# ---------------------------------------------------------------------------


def load_config_file(path: Path) -> dict[str, Any]:
    """Load a TOML config file as a flat ``{"section.key": value}`` dict.

    The on-disk shape is grouped sections::

        [expman]
        sla_window_hours = 24
        compliance_recipient = "@compliance:example-org"

    The flat shape we return is what the registry's schema keys use::

        {"expman.sla_window_hours": 24,
         "expman.compliance_recipient": "@compliance:example-org"}
    """
    from axiom.infra.config_loader import load_toml

    raw = load_toml(path) if path.exists() else {}
    flat: dict[str, Any] = {}
    _flatten(raw, prefix="", out=flat)
    return flat


def _flatten(d: dict, *, prefix: str, out: dict[str, Any]) -> None:
    for k, v in d.items():
        key = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            _flatten(v, prefix=key, out=out)
        else:
            out[key] = v


# ---------------------------------------------------------------------------
# Watcher protocol
# ---------------------------------------------------------------------------


class Watcher(Protocol):
    def start(self) -> None: ...
    def stop(self) -> None: ...


# ---------------------------------------------------------------------------
# Polling backend (fallback; also primary in tests)
# ---------------------------------------------------------------------------


@dataclass
class PollingWatcher:
    """Polls the directory at ``interval`` seconds.

    Sufficient for self-hosted nodes where ``watchdog`` is unavailable or
    blocked by SELinux / AppArmor profiles. Default 1s interval.
    """

    directory: Path
    apply_fn: Any
    """``Callable[[Path, dict[str, Any]], None]``."""
    interval: float = 1.0
    _stop_event: threading.Event = field(default_factory=threading.Event)
    _thread: threading.Thread | None = None
    _last_mtimes: dict[Path, float] = field(default_factory=dict)

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="config-polling-watcher"
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=self.interval * 3)
            self._thread = None

    def poll_once(self) -> None:
        """Run one polling pass. Public so tests can drive it directly."""
        if not self.directory.exists():
            return
        for p in self.directory.glob("*.toml"):
            try:
                mtime = p.stat().st_mtime
            except OSError:
                continue
            last = self._last_mtimes.get(p)
            if last != mtime:
                self._last_mtimes[p] = mtime
                self._safe_apply(p)

    def _loop(self) -> None:
        # Seed mtimes without firing to avoid re-applying on first start.
        if self.directory.exists():
            for p in self.directory.glob("*.toml"):
                try:
                    self._last_mtimes[p] = p.stat().st_mtime
                except OSError:
                    pass
        while not self._stop_event.is_set():
            self.poll_once()
            self._stop_event.wait(self.interval)

    def _safe_apply(self, path: Path) -> None:
        try:
            values = load_config_file(path)
            self.apply_fn(path, values)
        except Exception:
            log.exception("config watcher: failed to apply %s", path)


# ---------------------------------------------------------------------------
# watchdog backend (default in production)
# ---------------------------------------------------------------------------


def _try_watchdog_backend(
    directory: Path, apply_fn: Any
) -> Watcher | None:
    try:
        from watchdog.events import FileSystemEventHandler
        from watchdog.observers import Observer
    except Exception:
        return None

    class _Handler(FileSystemEventHandler):
        def __init__(self, apply_fn: Any) -> None:
            self._apply_fn = apply_fn
            # Debounce: dedupe rapid-fire events per-path within window.
            self._last_seen: dict[str, float] = {}
            self._debounce_s = 0.2

        def _emit(self, src_path: str) -> None:
            p = Path(src_path)
            if p.suffix != ".toml":
                return
            now = time.monotonic()
            last = self._last_seen.get(src_path, 0.0)
            if now - last < self._debounce_s:
                return
            self._last_seen[src_path] = now
            try:
                values = load_config_file(p)
                self._apply_fn(p, values)
            except Exception:
                log.exception(
                    "config watcher: failed to apply %s", src_path
                )

        def on_modified(self, event: Any) -> None:
            if not event.is_directory:
                self._emit(event.src_path)

        def on_created(self, event: Any) -> None:
            if not event.is_directory:
                self._emit(event.src_path)

    @dataclass
    class _WatchdogWatcher:
        directory: Path
        _observer: Any = None
        _handler: Any = None

        def start(self) -> None:
            if self._observer is not None:
                return
            self.directory.mkdir(parents=True, exist_ok=True)
            self._handler = _Handler(apply_fn)
            self._observer = Observer()
            self._observer.schedule(
                self._handler, str(self.directory), recursive=False
            )
            self._observer.start()

        def stop(self) -> None:
            if self._observer is not None:
                self._observer.stop()
                self._observer.join(timeout=2.0)
                self._observer = None

    return _WatchdogWatcher(directory=directory)


# ---------------------------------------------------------------------------
# Facade
# ---------------------------------------------------------------------------


def make_watcher(
    directory: Path,
    apply_fn: Any,
    *,
    prefer_polling: bool = False,
) -> Watcher:
    """Choose a backend automatically.

    Pass ``prefer_polling=True`` in tests for deterministic behavior.
    """
    if not prefer_polling:
        w = _try_watchdog_backend(directory, apply_fn)
        if w is not None:
            return w
    return PollingWatcher(directory=directory, apply_fn=apply_fn)


__all__ = [
    "PollingWatcher",
    "Watcher",
    "load_config_file",
    "make_watcher",
]
