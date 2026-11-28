# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Session provider seam for the attest schema (fleet's pattern, ADR-052).

Production binds ``axiom.infra.db.session_for('attest')``. The caller commits.
"""

from __future__ import annotations

import contextlib
import contextvars
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager
from typing import Any


def _default_provider() -> AbstractContextManager[Any]:
    from axiom.infra.db import session_for

    return session_for("attest")


_provider: Callable[[], AbstractContextManager[Any]] = _default_provider


def set_provider(provider: Callable[[], AbstractContextManager[Any]]) -> None:
    global _provider
    _provider = provider


def reset_provider() -> None:
    global _provider
    _provider = _default_provider


#: The session a read path shares with the helpers it calls (see :func:`shared_session`).
_shared: contextvars.ContextVar[Any] = contextvars.ContextVar("attest_shared_session", default=None)


@contextlib.contextmanager
def session_scope() -> Iterator[Any]:
    """Yield a Session from the bound provider. Caller commits. Inside :func:`shared_session` it
    yields that session instead of opening another."""
    shared = _shared.get()
    if shared is not None:
        yield shared
        return
    with _provider() as session:
        yield session


@contextlib.contextmanager
def shared_session() -> Iterator[Any]:
    """One session for a read path made of many helpers (#2422). Each ``session_scope()`` taken
    while it is open, however deep, joins it. A session costs a pooled connection, a schema check
    and its own commit, and a search that took six of them spent most of its time there.

    Use it for a path that reads and refreshes its own projection: a joined scope is not isolated,
    so an error a helper swallows after a failed statement leaves the shared transaction aborted.
    Helpers that probe with ``to_regclass`` rather than catching a missing table are safe."""
    shared = _shared.get()
    if shared is not None:
        yield shared
        return
    with _provider() as session:
        token = _shared.set(session)
        try:
            yield session
        finally:
            _shared.reset(token)
