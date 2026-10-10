# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Where the memory layer keeps its ledger and its recall corpus (ADR-174).

Production: Postgres. A *location* (the user's memory directory, a classroom, a
runtime scope) names a ledger scope, so one database serves every location and
two locations never share rows. Tests bind an in-memory seam and, for the recall
corpus, a SQLite factory through ``set_recall_factory``; nothing else in the
runtime uses SQLite.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from pathlib import Path
from typing import Any

from axiom.memory.pg_store import MemoryStoreUnavailable, ensure_provisioned


def scope_for(kind: str, root: str | Path) -> str:
    """A stable ledger scope for a location, e.g. ``user:3fa9c1d2e4b7``."""
    digest = hashlib.sha256(str(Path(root).expanduser().resolve()).encode("utf-8")).hexdigest()
    return f"{kind}:{digest[:12]}"


def open_ledger(kind: str, root: str | Path):
    """An ``ArtifactRegistry`` over the Postgres ledger scope for this location."""
    from axiom.artifacts.pg_backend import open_backend
    from axiom.artifacts.registry import ArtifactRegistry

    return ArtifactRegistry(backend=open_backend(scope_for(kind, root)))


def open_graph_for(kind: str, root: str | Path):
    from axiom.memory.pg_graph import open_graph

    return open_graph(scope_for(kind, root))


_recall_factory: Callable[[Path], Any] | None = None


def set_recall_factory(factory: Callable[[Path], Any] | None) -> None:
    """Tests bind a factory (a SQLite store under a tmp dir); production leaves it unset."""
    global _recall_factory
    _recall_factory = factory


def open_recall_store(base: Path):
    """The retrieval store holding the recall corpus: the Postgres RAG store.

    The corpus is named ``rag-memory:<principal>``, so the shared store keeps one
    principal's memory apart from every other corpus.
    """
    if _recall_factory is not None:
        return _recall_factory(base)
    from axiom.infra.db import libpq_url, platform_db_url
    from axiom.rag.store_factory import create_store

    ensure_provisioned()  # the same database, so the same plain advice if it is not there
    try:
        return create_store(libpq_url(platform_db_url()))
    except Exception as exc:  # noqa: BLE001
        raise MemoryStoreUnavailable(
            f"the recall corpus needs Postgres and could not use it ({type(exc).__name__}: {exc}). "
            "Postgres is the runtime database (ADR-174)."
        ) from exc
