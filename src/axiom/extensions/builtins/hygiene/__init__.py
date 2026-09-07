# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Resource stewardship and system hygiene.

Houses **TIDY**, the infrastructure-steward agent (named after the
Micro-Obliterator from AXI). TIDY monitors vitals, enforces retention,
provisions infrastructure, manages model lifecycle, and maintains
federation peer liveness. See ``agents/tidy/persona.md`` for TIDY's role.

Public API:
    acquire(owner, suffix, purpose, retention) -> Path | None
    acquire_dir(owner, purpose, retention) -> Path | None
    scratch_file(owner, suffix, purpose) -> context manager yielding Path | None
    scratch_dir(owner, purpose) -> context manager yielding Path | None
    manager() -> MoManager (process-global singleton)
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .manager import MoManager

_lock = threading.Lock()
_instance: MoManager | None = None


def manager() -> MoManager:
    """Return the process-global MoManager singleton."""
    global _instance
    if _instance is None:
        with _lock:
            if _instance is None:
                _instance = MoManager()
    return _instance


def acquire(
    owner: str,
    suffix: str = "",
    purpose: str = "",
    retention: str = "session",
) -> Path | None:
    """Acquire a managed scratch file. Returns None if unavailable."""
    return manager().acquire_file(owner, suffix=suffix, purpose=purpose, retention=retention)


def acquire_dir(
    owner: str,
    purpose: str = "",
    retention: str = "session",
) -> Path | None:
    """Acquire a managed scratch directory. Returns None if unavailable."""
    return manager().acquire_dir(owner, purpose=purpose, retention=retention)


@contextmanager
def scratch_file(
    owner: str,
    suffix: str = "",
    purpose: str = "",
) -> Iterator[Path | None]:
    """Context manager: acquire a transient scratch file, auto-release on exit."""
    mgr = manager()
    path = mgr.acquire_file(owner, suffix=suffix, purpose=purpose, retention="transient")
    try:
        yield path
    finally:
        if path is not None:
            mgr.release(path)


@contextmanager
def scratch_dir(
    owner: str,
    purpose: str = "",
) -> Iterator[Path | None]:
    """Context manager: acquire a transient scratch directory, auto-release on exit."""
    mgr = manager()
    path = mgr.acquire_dir(owner, purpose=purpose, retention="transient")
    try:
        yield path
    finally:
        if path is not None:
            mgr.release(path)
