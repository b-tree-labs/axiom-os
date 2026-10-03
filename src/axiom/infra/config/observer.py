# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Key-scoped subscriber pattern for ``axiom.infra.config``.

``observe(key, callback)`` registers a callback that fires when the
key's value changes. Per AEOS §2.13: subscribers see ``(old, new,
source)``; the callback is responsible for being idempotent + fast
(real work belongs in the calling agent's next-fire).

Internally implemented as a registry-wide listener that filters per
key — keeps the registry's contract simple (one global change stream)
and lets observers be added/removed without re-wiring the registry.
"""

from __future__ import annotations

import threading
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Callable

from axiom.infra.config.registry import (
    ChangeRecord,
    get_registry,
)

ObserverCallback = Callable[[object, object, str], None]
"""Signature: ``(old_value, new_value, source) -> None``."""


@dataclass
class ObserverRegistry:
    """Maps config key → list of callbacks. One per registry."""

    _by_key: dict[str, list[ObserverCallback]] = field(
        default_factory=lambda: defaultdict(list)
    )
    _lock: threading.RLock = field(default_factory=threading.RLock)

    def subscribe(
        self, key: str, callback: ObserverCallback
    ) -> Callable[[], None]:
        """Register a callback and return a deregister handle."""
        with self._lock:
            self._by_key[key].append(callback)

        def _unsubscribe() -> None:
            with self._lock:
                try:
                    self._by_key[key].remove(callback)
                except ValueError:
                    pass

        return _unsubscribe

    def fire(self, record: ChangeRecord) -> None:
        """Dispatch a change record to all subscribers for that key."""
        with self._lock:
            callbacks = list(self._by_key.get(record.key, []))
        for fn in callbacks:
            try:
                fn(record.old_value, record.new_value, record.source)
            except Exception:
                # Listener failures don't propagate — hygiene watchers
                # surface persistent failures as findings.
                pass


# ---------------------------------------------------------------------------
# Module plumbing — one ObserverRegistry per ConfigRegistry singleton
# ---------------------------------------------------------------------------

_wired_lock = threading.Lock()
_observer_singleton: ObserverRegistry | None = None


def get_observer_registry() -> ObserverRegistry:
    """Lazily attach to the current ConfigRegistry."""
    global _observer_singleton
    if _observer_singleton is None:
        with _wired_lock:
            if _observer_singleton is None:
                obs = ObserverRegistry()
                get_registry().add_listener(obs.fire)
                _observer_singleton = obs
    return _observer_singleton


def reset_for_testing() -> None:
    global _observer_singleton
    with _wired_lock:
        _observer_singleton = None


def observe(key: str, callback: ObserverCallback) -> Callable[[], None]:
    """Public API — register a callback for a key's value changes.

    Returns a deregister function::

        unsub = observe("expman.sla_hours", lambda old, new, src: ...)
        # ... later ...
        unsub()
    """
    return get_observer_registry().subscribe(key, callback)


__all__ = [
    "ObserverCallback",
    "ObserverRegistry",
    "get_observer_registry",
    "observe",
    "reset_for_testing",
]
