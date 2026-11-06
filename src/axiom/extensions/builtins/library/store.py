# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Session and retrieval-store seams for the library (the receipts pattern).

Production binds ``axiom.infra.db.session_for('library')`` (ADR-052) and the
node's own retrieval store (``axiom.rag.store_factory``). Tests bind SQLite for
both; production never does (ADR-174). These two providers are the only
injection points, so every surface goes through the same ones.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager
from typing import Any


def _default_provider() -> AbstractContextManager[Any]:
    from axiom.infra.db import session_for

    return session_for("library")


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


_rag_store: Any = None
_rag_override: Callable[[], Any] | None = None


def set_rag_store(factory: Callable[[], Any] | None) -> None:
    """Bind the retrieval store (tests inject a SQLite store; ``None`` resets)."""
    global _rag_override, _rag_store
    _rag_override = factory
    _rag_store = None


def rag_store() -> Any:
    """The node's retrieval store, or ``None`` when this install has none.

    ``None`` is a real answer: the document still lands in bronze and the
    catalog, and is marked ``pending`` so a later index pass picks it up. A
    library that refused uploads because retrieval was down would lose the
    document to save a search index.
    """
    global _rag_store
    if _rag_override is not None:
        return _rag_override()
    if _rag_store is not None:
        return _rag_store
    try:
        from axiom.rag.store_factory import create_store, resolve_store_url

        url = resolve_store_url().url
        if not url or url.startswith(("http://", "https://")):
            # A remote store is a bound install's: its uploads go to the node,
            # which indexes them there (see forward.py).
            return None
        store = create_store(url)
        store.connect()
    except Exception:  # noqa: BLE001 - no store is "pending", never a crash
        return None
    _rag_store = store
    return store


__all__ = ["rag_store", "reset_provider", "session_scope", "set_provider", "set_rag_store"]
