# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Session provider seam for the fleet schema (schedule's pattern).

Production binds ``axiom.infra.db.session_for('fleet')`` (ADR-052); tests
bind SQLite. The provider is the only injection point — ingest, views,
skills and the API all go through ``session_scope()``. Caller commits.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager
from typing import Any


def _default_provider() -> AbstractContextManager[Any]:
    # Lazy import so unit tests that bind their own provider never touch
    # the Postgres-backed db module.
    from axiom.infra.db import session_for

    return session_for("fleet")


_provider: Callable[[], AbstractContextManager[Any]] = _default_provider


def set_provider(provider: Callable[[], AbstractContextManager[Any]]) -> None:
    """Bind the session provider (tests inject SQLite here)."""
    global _provider
    _provider = provider


def reset_provider() -> None:
    global _provider
    _provider = _default_provider


@contextlib.contextmanager
def session_scope() -> Iterator[Any]:
    """Yield a Session from the bound provider. Caller commits."""
    with _provider() as session:
        yield session
