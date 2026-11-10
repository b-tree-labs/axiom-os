# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Override storage: the ``flag_overrides`` table behind one session seam.

Production binds ``axiom.infra.db.session_for("flags")`` (ADR-052) against the
runtime Postgres (ADR-174); tests bind SQLite through :func:`set_provider`.
The service, the skills and the HTTP route all reach the table through
:class:`OverrideStore`, and nothing else opens a session on it.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from typing import Any

from .db_models import SCOPES, FlagOverride


def _default_provider() -> AbstractContextManager[Any]:
    from axiom.infra.db import session_for

    return session_for("flags")


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
    """A session from the bound provider. Caller commits."""
    with _provider() as session:
        yield session


def _check_scope(scope: str) -> None:
    if scope not in SCOPES:
        raise ValueError(f"scope is one of {', '.join(SCOPES)}, not {scope!r}")


class OverrideStore:
    """Reads and writes ``flag_overrides`` rows."""

    def overrides_for(self, scope: str, scope_id: str | None) -> dict[str, str]:
        """``{key: value}`` stored for one node, account or user; empty when unnamed."""
        _check_scope(scope)
        if not scope_id:
            return {}
        with session_scope() as s:
            rows = s.query(FlagOverride).filter_by(scope=scope, scope_id=str(scope_id)).all()
            return {r.key: r.value for r in rows}

    def put(self, scope: str, scope_id: str, key: str, value: str, *, by: str) -> dict[str, Any]:
        """Upsert one override; returns the stored row."""
        _check_scope(scope)
        with session_scope() as s:
            row = s.get(FlagOverride, (scope, str(scope_id), key))
            now = datetime.now(UTC)
            if row is None:
                row = FlagOverride(scope=scope, scope_id=str(scope_id), key=key)
                s.add(row)
            row.value, row.updated_by, row.updated_at = value, by, now
            s.commit()
            return _row(row)

    def delete(self, scope: str, scope_id: str, key: str) -> bool:
        """Remove one override; ``True`` when there was one."""
        _check_scope(scope)
        with session_scope() as s:
            row = s.get(FlagOverride, (scope, str(scope_id), key))
            if row is None:
                return False
            s.delete(row)
            s.commit()
            return True

    def rows(self, key: str | None = None) -> list[dict[str, Any]]:
        """Every stored override (optionally for one key), scope-ordered."""
        with session_scope() as s:
            q = s.query(FlagOverride)
            if key:
                q = q.filter_by(key=key)
            rows = [_row(r) for r in q.all()]
        order = {sc: i for i, sc in enumerate(SCOPES)}
        return sorted(rows, key=lambda r: (r["key"], order[r["scope"]], r["scope_id"]))


def _row(r: FlagOverride) -> dict[str, Any]:
    at = r.updated_at
    return {
        "scope": r.scope,
        "scope_id": r.scope_id,
        "key": r.key,
        "value": r.value,
        "updated_by": r.updated_by,
        "updated_at": at.isoformat() if at is not None else None,
    }


__all__ = ["OverrideStore", "reset_provider", "session_scope", "set_provider"]
