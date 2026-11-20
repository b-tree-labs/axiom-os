# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""One session seam per extension schema (ADR-052, ADR-174).

Production binds ``session_for(<extension>)``; tests bind a SQLite session so the code runs
without a server. Each extension that persists through Postgres owns one ``SchemaSeam``
instead of carrying its own copy of the provider, provisioning and error handling.
"""

from __future__ import annotations

import contextlib
import threading
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager
from typing import Any


class StoreUnavailable(RuntimeError):
    """Postgres is not configured, reachable, or provisioned for this schema."""


class SchemaSeam:
    def __init__(self, extension: str, *, what: str) -> None:
        self.extension = extension
        self.what = what
        self._provider: Callable[[], AbstractContextManager[Any]] | None = None
        self._provisioned = False
        self._lock = threading.Lock()

    def _default_provider(self) -> AbstractContextManager[Any]:
        from axiom.infra.db import session_for

        return session_for(self.extension)

    def set_provider(self, provider: Callable[[], AbstractContextManager[Any]]) -> None:
        """Bind a session provider (tests inject SQLite). A bound provider owns its schema."""
        self._provider = provider
        self._provisioned = True

    def reset_provider(self) -> None:
        self._provider = None
        self._provisioned = False

    def ensure_provisioned(self) -> None:
        """Bring the schema to head once per process, or say plainly why not."""
        if self._provisioned:
            return
        with self._lock:
            if self._provisioned:
                return
            try:
                from axiom.infra.db import provision_extension

                result = provision_extension(self.extension)
            except Exception as exc:  # noqa: BLE001
                raise StoreUnavailable(self._advice(f"{type(exc).__name__}: {exc}")) from exc
            if not result.ok:
                raise StoreUnavailable(self._advice(result.error or "provisioning failed"))
            self._provisioned = True

    def _advice(self, detail: str) -> str:
        return (
            f"{self.what} needs Postgres and could not use it ({detail}). Start the local stack "
            "with the infra command, or set AXIOM_DB_URL to a Postgres you run; Postgres is the "
            "runtime database (ADR-174)."
        )

    @contextlib.contextmanager
    def session_scope(self) -> Iterator[Any]:
        """Yield a Session from the bound provider. Caller commits."""
        self.ensure_provisioned()
        with (self._provider or self._default_provider)() as session:
            yield session
