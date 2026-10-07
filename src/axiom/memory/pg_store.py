# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The session seam between the memory layer and its Postgres schema.

Production binds a provider wrapping ``axiom.infra.db.session_for('memory')``
(ADR-052). Tests bind a provider that yields a SQLite session, so the ledger and
graph are exercised without a server (the policy allows SQLite in tests only;
see ADR-174). The provider is the only injection point.
"""

from __future__ import annotations

import contextlib
import threading
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager
from typing import Any


class MemoryStoreUnavailable(RuntimeError):
    """Postgres is not configured, reachable, or provisioned for the memory layer."""


def _default_provider() -> AbstractContextManager[Any]:
    from axiom.infra.db import session_for

    return session_for("memory")


_provider: Callable[[], AbstractContextManager[Any]] = _default_provider
_provisioned = False
_lock = threading.Lock()


def set_provider(provider: Callable[[], AbstractContextManager[Any]]) -> None:
    """Bind the session provider (tests inject SQLite here)."""
    global _provider, _provisioned
    _provider = provider
    _provisioned = True  # a test-bound provider owns its own schema


def reset_provider() -> None:
    global _provider, _provisioned
    _provider = _default_provider
    _provisioned = False


def ensure_provisioned() -> None:
    """Bring the ``memory`` schema to head once per process, or say plainly why not."""
    global _provisioned
    if _provisioned:
        return
    with _lock:
        if _provisioned:
            return
        try:
            from axiom.infra.db import provision_extension

            result = provision_extension("memory")
        except Exception as exc:  # noqa: BLE001
            raise MemoryStoreUnavailable(_advice(f"{type(exc).__name__}: {exc}")) from exc
        if not result.ok:
            raise MemoryStoreUnavailable(_advice(result.error or "provisioning failed"))
        _provisioned = True


def _advice(detail: str) -> str:
    return (
        "the memory layer needs Postgres and could not use it "
        f"({detail}). Start the local stack with the infra command, or set AXIOM_DB_URL to a "
        "Postgres you run; Postgres is the runtime database (ADR-174)."
    )


@contextlib.contextmanager
def session_scope() -> Iterator[Any]:
    """Yield a Session from the bound provider. Caller commits."""
    ensure_provisioned()
    with _provider() as session:
        yield session
