# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Session provider seam for the receipts schema (fleet's pattern).

Production binds ``axiom.infra.db.session_for('receipts')`` (ADR-052);
tests bind SQLite. The provider is the only injection point — the brief
composer, skills and MCP handlers all go through ``session_scope()``.
Caller commits.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager
from typing import Any


def _default_provider() -> AbstractContextManager[Any]:
    from axiom.infra.db import session_for

    return session_for("receipts")


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
