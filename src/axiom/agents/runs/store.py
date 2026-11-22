# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Where run handles live.

A small interface and an in-memory implementation. The durable store is
deliberately NOT chosen here (ADR-233 part 10 wants a durable, owner-aware
store that survives a restart; which substrate carries it is an open question
for the A0 PR). Anything that satisfies :class:`RunStore` can replace the
in-memory one without touching the manager.
"""

from __future__ import annotations

import threading
from typing import Protocol

from axiom.agents.runs.handle import RunEvent, RunHandle, RunState


class RunStore(Protocol):
    def put(self, handle: RunHandle) -> None: ...

    def get(self, run_id: str) -> RunHandle | None: ...

    def list(
        self, *, state: RunState | None = None, parent_id: str | None = None
    ) -> list[RunHandle]: ...

    def append_event(self, event: RunEvent) -> None: ...

    def events(self, run_id: str) -> list[RunEvent]: ...


class InMemoryRunStore:
    """Process-local, thread-safe. Lost on restart by design; see module docstring."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._runs: dict[str, RunHandle] = {}
        self._events: dict[str, list[RunEvent]] = {}

    def put(self, handle: RunHandle) -> None:
        with self._lock:
            self._runs[handle.id] = handle

    def get(self, run_id: str) -> RunHandle | None:
        with self._lock:
            return self._runs.get(run_id)

    def list(
        self, *, state: RunState | None = None, parent_id: str | None = None
    ) -> list[RunHandle]:
        with self._lock:
            runs = list(self._runs.values())
        if state is not None:
            runs = [r for r in runs if r.state is state]
        if parent_id is not None:
            runs = [r for r in runs if r.parent_id == parent_id]
        return sorted(runs, key=lambda r: r.created_at)

    def append_event(self, event: RunEvent) -> None:
        with self._lock:
            self._events.setdefault(event.run_id, []).append(event)

    def events(self, run_id: str) -> list[RunEvent]:
        with self._lock:
            return list(self._events.get(run_id, ()))
