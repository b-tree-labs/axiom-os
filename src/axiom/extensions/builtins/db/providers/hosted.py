# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Hosted PostgreSQL deployment provider for `axi db`.

For environments where PostgreSQL is managed externally (RDS, Cloud
SQL, on-prem hosted, staging clusters, etc.). Lifecycle commands
are no-ops — the database is not under `axi db`'s control. `status()`
attempts a connectivity ping.

Useful in CI, staging, and production: same `axi db status` UX,
different backend.
"""

from __future__ import annotations

import os
import re
from typing import Any

from axiom.extensions.builtins.db.providers.base import (
    DeploymentStatus,
    register_provider,
)


def _resolve_env_vars(value: str) -> str:
    """Expand ${VAR} references in a config string."""

    def _sub(match: re.Match[str]) -> str:
        var = match.group(1)
        return os.environ.get(var, match.group(0))

    return re.sub(r"\$\{([A-Z_][A-Z0-9_]*)\}", _sub, value)


class HostedProvider:
    """PostgreSQL managed outside of `axi db`.

    `connection_string` may include `${ENV_VAR}` references that get
    expanded from the environment at provider-construction time.
    Falls back to `AXIOM_DB_URL` env var if no connection_string is
    configured.
    """

    name = "hosted"

    def __init__(
        self,
        connection_string: str | None = None,
        **_: Any,
    ) -> None:
        if connection_string is None:
            connection_string = os.environ.get("AXIOM_DB_URL", "")
        self.connection_string = _resolve_env_vars(connection_string) if connection_string else ""

    def _no_op(self, verb: str) -> bool:
        if not self.connection_string:
            print(
                "  ✗ No connection_string configured for hosted backend. "
                "Set [db.deployment.hosted] connection_string in the manifest "
                "or AXIOM_DB_URL env var."
            )
            return False
        print(
            f"  ℹ Hosted backend: {verb} is a no-op. The database is managed "
            "externally."
        )
        return True

    def up(self) -> bool:
        return self._no_op("up")

    def down(self) -> bool:
        return self._no_op("down")

    def delete(self) -> bool:
        # Refuse outright — deleting a hosted DB shouldn't happen
        # through this CLI.
        print(
            "  ✗ Hosted backend: refusing to delete an externally-managed "
            "database. Use the cloud provider's tooling."
        )
        return False

    def status(self) -> DeploymentStatus:
        if not self.connection_string:
            return DeploymentStatus(
                backend=self.name,
                available=False,
                running=False,
                connection_url=None,
                extra={"reason": "no connection_string configured"},
            )

        # Try a lightweight connect to confirm reachability.
        try:
            from sqlalchemy import create_engine, text

            engine = create_engine(self.connection_string, pool_pre_ping=True)
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            engine.dispose()
            running = True
            reason = None
        except Exception as exc:  # noqa: BLE001 — surfacing to user
            running = False
            reason = str(exc)

        return DeploymentStatus(
            backend=self.name,
            available=True,
            running=running,
            connection_url=self.connection_string,
            extra={"reason": reason} if reason else {},
        )


register_provider("hosted", HostedProvider)
