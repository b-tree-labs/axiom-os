# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The store seam for the serving catalog.

Production binds ``axiom.infra.db.session_for("webapp")`` per ADR-052, so the
tables land in the extension's own schema and never in ``public``. Tests bind a
SQLite session instead, which is why the binding is a provider rather than a
module-level engine.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Iterator
from typing import Any

from axiom.extensions.builtins.webapp.catalog.models import Base, SiteCatalogChannel


def _default_provider() -> Any:
    from axiom.infra.db import session_for

    return session_for("webapp")


_provider: Callable[[], Any] = _default_provider


def bind(provider: Callable[[], Any]) -> None:
    """Point the store at a different session source (tests, embedding hosts)."""
    global _provider
    _provider = provider


@contextlib.contextmanager
def session_scope() -> Iterator[Any]:
    with _provider() as session:
        yield session


def ensure_tables(session) -> None:
    """Create the serving tables in the bound schema (idempotent).

    Uses the session's *connection*, not the engine: ``session_for`` sets
    ``search_path`` to the extension schema on that connection, so the tables
    land in ``webapp``. The engine alone would default to ``public``.
    """
    Base.metadata.create_all(bind=session.connection())


def read_channels(session, site: str) -> list[dict]:
    """A site's channels — an indexed read, never an aggregate over gold."""
    q = (
        session.query(SiteCatalogChannel)
        .filter(SiteCatalogChannel.site == site)
        .order_by(SiteCatalogChannel.stream, SiteCatalogChannel.channel)
    )
    return [
        {
            "stream": c.stream,
            "channel": c.channel,
            "unit": c.unit,
            "rows": c.rows,
            "first": c.first_ts,
            "last": c.last_ts,
        }
        for c in q
    ]


def read_site_summaries(session) -> list[dict]:
    """Per-site rollup: channel count, row count, coverage span."""
    from sqlalchemy import func

    q = (
        session.query(
            SiteCatalogChannel.site,
            func.count().label("channels"),
            func.coalesce(func.sum(SiteCatalogChannel.rows), 0).label("rows"),
            func.min(SiteCatalogChannel.first_ts).label("first"),
            func.max(SiteCatalogChannel.last_ts).label("last"),
        )
        .group_by(SiteCatalogChannel.site)
        .order_by(SiteCatalogChannel.site)
    )
    return [
        {
            "site": r.site,
            "channels": int(r.channels),
            "rows": int(r.rows),
            "first": r.first or "",
            "last": r.last or "",
        }
        for r in q
    ]


__all__ = [
    "bind",
    "ensure_tables",
    "read_channels",
    "read_site_summaries",
    "session_scope",
]
